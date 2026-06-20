"""VITALS — ECS services on the monitor.

Each ECS service is a patient on a bedside monitor. A live ECG trace scrolls
across the screen and its heartbeat rate tracks the service's CPU load — a busy
service races, an idle one ticks along. A memory bar rides alongside, and the
running/desired count sits at the end of the line. When a service loses all its
tasks the trace flatlines and the alarm sounds; when it's merely under strength
it's flagged critical. A glance tells you who's healthy, who's working hard, and
who's coding blue.

Data: `aws ecs` (describe-services) + CloudWatch (AWS/ECS CPU/Memory). Uses your
ambient AWS credentials; set cluster/region/profile in the manifest. No secrets
are stored here.
"""

import curses
import json

from driftcore import (
    board, Scene, put, putch, center, clamp, run, cp,
    C_RED, C_GREEN, C_YELLOW, C_CYAN, C_WHITE,
)

BASE = "▁"                       # ECG baseline
PULSE = ["▂", "▄", "█", "▅", "▂"]   # one heartbeat blip
FLAT = "─"
TRACE_MAX = 400                  # internal history width; we display a slice


@board
class VitalsScene(Scene):
    name = "vitals"
    title = "♥ V I T A L S ♥"
    interval = 60.0
    CONFIG = {
        "region": {"default": "us-east-1"},
        "cluster": {"default": None},        # None = every cluster in the region
        "profile": {"default": None},
        "strip_prefix": {"default": ""},
        "exclude": {"default": []},          # service-name substrings to hide
        "max_services": {"default": 12},
    }

    @classmethod
    def available(cls, tele, cfg=None):
        return bool(run(["aws", "--version"], 4))

    # ---- data (background thread) -----------------------------------------
    def _aws(self, cfg, args, timeout=20):
        base = ["aws"]
        if cfg.get("profile"):
            base += ["--profile", cfg["profile"]]
        base += ["--region", cfg.get("region") or "us-east-1", "--output", "json"]
        out = run(base + args, timeout)
        if not out:
            return None
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return None

    def _ecs_metric(self, cfg, metric, cluster, service):
        data = self._aws(cfg, [
            "cloudwatch", "get-metric-statistics",
            "--namespace", "AWS/ECS", "--metric-name", metric,
            "--dimensions", f"Name=ClusterName,Value={cluster}",
            f"Name=ServiceName,Value={service}",
            "--start-time", _iso(600), "--end-time", _iso(0),
            "--period", "300", "--statistics", "Average",
        ], 15)
        pts = (data or {}).get("Datapoints", [])
        if not pts:
            return None
        pts.sort(key=lambda p: p.get("Timestamp", ""))
        return pts[-1].get("Average")

    def fetch(self, cfg):
        if cfg.get("cluster"):
            clusters = [cfg["cluster"]]
        else:
            data = self._aws(cfg, ["ecs", "list-clusters"])
            clusters = [a.split("/")[-1] for a in (data or {}).get("clusterArns", [])]
        if not clusters:
            return {"vitals": {"error": "no ECS clusters / no AWS access"}}

        services = []
        for cl in clusters:
            arns_data = self._aws(cfg, ["ecs", "list-services",
                                        "--cluster", cl, "--max-items", "100"])
            arns = [a.split("/")[-1] for a in (arns_data or {}).get("serviceArns", [])]
            for i in range(0, len(arns), 10):
                batch = arns[i:i + 10]
                if not batch:
                    continue
                desc = self._aws(cfg, ["ecs", "describe-services",
                                       "--cluster", cl, "--services", *batch], 25)
                for s in (desc or {}).get("services", []):
                    evmsg = ((s.get("events") or [{}])[0]).get("message", "")
                    failing = any(t in evmsg.lower()
                                  for t in ("unable", "fail", "error", "cannot"))
                    services.append({
                        "name": s.get("serviceName", "?"), "cluster": cl,
                        "desired": int(s.get("desiredCount", 0)),
                        "running": int(s.get("runningCount", 0)),
                        "pending": int(s.get("pendingCount", 0)),
                        "failing": failing,
                    })
        # drop excluded services (case-insensitive substring match)
        excl = [e.lower() for e in (cfg.get("exclude") or [])]
        if excl:
            services = [s for s in services
                        if not any(e in s["name"].lower() for e in excl)]
        # critical first (down / under-strength / failing), then by name
        services.sort(key=lambda s: (s["running"] >= s["desired"] and not s["failing"],
                                     s["name"]))
        cap = int(cfg.get("max_services") or 12)
        shown = services[:cap]
        # autoscaling envelope (min/max desired) per service, keyed by name
        targets = self._aws(cfg, ["application-autoscaling", "describe-scalable-targets",
                                  "--service-namespace", "ecs"])
        scale = {}
        for t in (targets or {}).get("ScalableTargets", []):
            scale[t.get("ResourceId", "").split("/")[-1]] = (
                t.get("MinCapacity"), t.get("MaxCapacity"))
        # utilization only for what we display, to bound CloudWatch calls
        for s in shown:
            s["min"], s["max"] = scale.get(s["name"], (None, None))
            s["cpu"] = self._ecs_metric(cfg, "CPUUtilization", s["cluster"], s["name"])
            s["mem"] = self._ecs_metric(cfg, "MemoryUtilization", s["cluster"], s["name"])
        return {"vitals": {"services": shown, "total": len(services),
                           "region": cfg.get("region") or "us-east-1"}}

    # ---- render -----------------------------------------------------------
    def build(self):
        self.t = 0.0
        self.trace = {}      # name -> list[str] (rolling ECG history)
        self.phase = {}      # name -> beat phase accumulator (0..1)
        self.pulse = {}      # name -> index into PULSE (>=len = no active blip)
        self.alarm_t = 0.0

    def _step_trace(self, s, dt):
        nm = s["name"]
        tr = self.trace.setdefault(nm, [BASE] * TRACE_MAX)
        down = s["running"] == 0
        if down:
            tr.append(FLAT)
        else:
            pi = self.pulse.get(nm, len(PULSE))
            if pi < len(PULSE):
                tr.append(PULSE[pi])
                self.pulse[nm] = pi + 1
            else:
                tr.append(BASE)
                cpu = (s.get("cpu") or 0.0) / 100.0
                hz = 0.6 + clamp(cpu) * 2.6          # beats/sec: idle≈0.6, hot≈3.2
                ph = self.phase.get(nm, 0.0) + dt * hz
                if ph >= 1.0:
                    ph = 0.0
                    self.pulse[nm] = 0               # fire a heartbeat
                self.phase[nm] = ph
        if len(tr) > TRACE_MAX:
            del tr[:len(tr) - TRACE_MAX]

    def update(self, dt, frame, st):
        self.t += dt
        self.alarm_t += dt
        for s in (st.get("vitals") or {}).get("services", []):
            self._step_trace(s, dt)

    def draw(self, scr, frame, st):
        v = st.get("vitals")
        if not v:
            center(scr, self.h // 2, "attaching the leads…", cp(C_CYAN) | curses.A_DIM)
            return
        if v.get("error"):
            center(scr, self.h // 2, f"⚠ {v['error']}", cp(C_RED) | curses.A_BOLD)
            return
        svcs = v.get("services", [])
        if not svcs:
            center(scr, self.h // 2, "no services on the ward", cp(C_CYAN) | curses.A_DIM)
            return

        up = sum(1 for s in svcs if s["running"] > 0)
        crit = sum(1 for s in svcs if s["running"] < s["desired"] or s["failing"])
        put(scr, 1, 3, f"WARD · {len(svcs)} monitors · {up}/{len(svcs)} with a pulse",
            cp(C_WHITE) | curses.A_BOLD)
        if crit:
            alert = f"⚠ {crit} CRITICAL"
            blink = curses.A_BOLD if int(self.alarm_t * 3) % 2 else curses.A_DIM
            put(scr, 1, max(3, self.w - len(alert) - 4), alert, cp(C_RED) | blink)

        prefix = self.cfg.get("strip_prefix") or ""
        content_top, content_bot = 3, self.h - 1
        avail = max(1, content_bot - content_top)
        n = len(svcs)
        slot = avail / n                       # vertical room per monitor (fills height)
        two_line = slot >= 2.0                 # stat line + full-width ECG when there's room
        readout_x = max(20, self.w - 40)
        for i, s in enumerate(svcs):
            y = content_top + int(i * slot)
            if y >= content_bot:
                break
            nm = s["name"]
            disp = nm[len(prefix):] if prefix and nm.startswith(prefix) else nm
            down = s["running"] == 0
            under = s["running"] < s["desired"]
            row_c = C_RED if down else (C_YELLOW if (under or s["failing"]) else C_GREEN)
            if two_line:
                put(scr, y, 3, disp[:readout_x - 4], cp(row_c) | curses.A_BOLD)
                self._draw_readout(scr, s, y, readout_x, row_c)
                if y + 1 < content_bot:
                    self._draw_trace(scr, s, y + 1, 3, self.w - 6, row_c)
            else:
                label_w = 18
                put(scr, y, 3, f"{disp[:label_w - 1]:<{label_w}}", cp(row_c) | curses.A_BOLD)
                tx = 3 + label_w
                self._draw_trace(scr, s, y, tx, max(4, readout_x - tx - 1), row_c)
                self._draw_readout(scr, s, y, readout_x, row_c)
        if v.get("total", 0) > len(svcs):
            put(scr, self.h - 1, 3, f"+{v['total'] - len(svcs)} more…",
                cp(C_WHITE) | curses.A_DIM)

    def _draw_trace(self, scr, s, y, x, w_, row_c):
        if w_ < 4:
            return
        tr = self.trace.get(s["name"], [BASE] * TRACE_MAX)
        trace_s = "".join(tr[-w_:])
        if s["running"] == 0:
            a = curses.A_BOLD if int(self.alarm_t * 4) % 2 else curses.A_DIM
            put(scr, y, x, trace_s, cp(C_RED) | a)
        else:
            put(scr, y, x, trace_s, cp(row_c) | curses.A_BOLD)

    @staticmethod
    def _util_color(pct):
        # mirrors the 60% CPU autoscaling target / 85% memory backstop thresholds
        if pct is None:
            return C_WHITE
        return C_GREEN if pct < 60 else (C_YELLOW if pct < 85 else C_RED)

    def _draw_readout(self, scr, s, y, rx, row_c):
        down = s["running"] == 0
        under = s["running"] < s["desired"]
        x = rx
        if down:
            a = curses.A_BOLD if int(self.alarm_t * 4) % 2 else curses.A_DIM
            put(scr, y, x, "FLATLINE", cp(C_RED) | a)
            x += 11
        else:
            cpu, mem = s.get("cpu"), s.get("mem")
            ctxt = "CPU  --%" if cpu is None else f"CPU {cpu:>3.0f}%"
            put(scr, y, x, ctxt, cp(self._util_color(cpu)) | curses.A_BOLD)
            x += len(ctxt) + 2
            mtxt = "MEM  --%" if mem is None else f"MEM {mem:>3.0f}%"
            put(scr, y, x, mtxt, cp(self._util_color(mem)) | curses.A_BOLD)
            x += len(mtxt) + 2
        cnt = f"{s['running']}/{s['desired']}"
        put(scr, y, x, cnt, cp(row_c) | curses.A_BOLD)
        x += len(cnt) + 1
        if under or s["failing"]:
            put(scr, y, x, "!", cp(C_RED) | curses.A_BOLD)
            x += 2
        # autoscaling envelope: [min-max], or a dim ·fixed· for unscaled services
        if s.get("max") is not None:
            put(scr, y, x, f"[{s.get('min')}–{s['max']}]", cp(C_CYAN) | curses.A_DIM)
        else:
            put(scr, y, x, "·fixed·", cp(C_WHITE) | curses.A_DIM)

    def hud(self, st):
        v = st.get("vitals") or {}
        svcs = v.get("services", [])
        if not svcs:
            return [("ecs", "…")]
        crit = sum(1 for s in svcs if s["running"] < s["desired"] or s["failing"])
        cpus = [s.get("cpu") for s in svcs if s.get("cpu") is not None]
        peak = f"{max(cpus):.0f}%" if cpus else "—"
        return [("region", v.get("region", "—")),
                ("monitors", str(v.get("total", len(svcs)))),
                ("critical", str(crit)),
                ("peak cpu", peak)]


def _iso(secs_ago=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=secs_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
