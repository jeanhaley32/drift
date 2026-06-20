"""CISTERN — RDS capacity vessels.

Every database is a tank, and the waterline is how full it is against its ceiling:
storage used vs. its max for provisioned instances, consumed ACU vs. its max for
Aurora Serverless v2. A calm blue tank has room; as it fills it warms through
amber to a red, rippling brim with an overflow warning. Bubbles rise with live
connections, so the busy tanks shimmer. A glance tells you who's running out of
room before they actually do.

Data: `aws rds` + CloudWatch. Uses your ambient AWS credentials; set the
region/profile in the manifest. No secrets are stored here.
"""

import curses
import json
import math
import random
from datetime import datetime, timedelta, timezone

from driftcore import (
    board, Scene, put, putch, center, clamp, run, cp,
    C_RED, C_GREEN, C_YELLOW, C_CYAN, C_WHITE, C_BLUE,
)


def _iso(secs_ago=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=secs_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@board
class CisternScene(Scene):
    name = "cistern"
    title = "≈ C I S T E R N ≈"
    interval = 120.0
    CONFIG = {
        "region": {"default": "us-east-1"},
        "profile": {"default": None},
        "name_filter": {"default": ""},     # substring match; "" = all
        "max_tanks": {"default": 8},
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

    def _metric(self, cfg, metric, dim_name, dim_val, stat="Average"):
        data = self._aws(cfg, [
            "cloudwatch", "get-metric-statistics",
            "--namespace", "AWS/RDS", "--metric-name", metric,
            "--dimensions", f"Name={dim_name},Value={dim_val}",
            "--start-time", _iso(900), "--end-time", _iso(0),
            "--period", "300", "--statistics", stat,
        ], 20)
        pts = (data or {}).get("Datapoints", [])
        if not pts:
            return None
        pts.sort(key=lambda p: p.get("Timestamp", ""))
        return pts[-1].get(stat)

    def fetch(self, cfg):
        flt = (cfg.get("name_filter") or "").lower()
        vessels = []

        inst = self._aws(cfg, ["rds", "describe-db-instances"])
        if inst is None:
            return {"cistern": {"error": "no RDS access / aws CLI failed"}}
        for db in inst.get("DBInstances", []):
            ident = db.get("DBInstanceIdentifier", "?")
            if flt and flt not in ident.lower():
                continue
            if db.get("DBInstanceClass") == "db.serverless":
                continue   # Aurora member — counted via its cluster below
            alloc = float(db.get("AllocatedStorage") or 0)        # GiB
            ceiling = float(db.get("MaxAllocatedStorage") or alloc or 1)
            free_b = self._metric(cfg, "FreeStorageSpace", "DBInstanceIdentifier", ident)
            used = max(0.0, alloc - free_b / (1024 ** 3)) if (free_b and alloc) else None
            conns = self._metric(cfg, "DatabaseConnections", "DBInstanceIdentifier", ident)
            vessels.append({
                "name": ident, "unit": "GB",
                "fill": clamp(used / ceiling) if used is not None else None,
                "used": used, "ceiling": ceiling, "conns": conns,
                "status": db.get("DBInstanceStatus", ""),
            })

        clus = self._aws(cfg, ["rds", "describe-db-clusters"]) or {}
        for c in clus.get("DBClusters", []):
            ident = c.get("DBClusterIdentifier", "?")
            if flt and flt not in ident.lower():
                continue
            sv2 = c.get("ServerlessV2ScalingConfiguration") or {}
            maxacu = float(sv2.get("MaxCapacity") or 0)
            if not maxacu:
                continue   # not Serverless v2 — no simple capacity notion
            acu = self._metric(cfg, "ServerlessDatabaseCapacity",
                               "DBClusterIdentifier", ident)
            conns = self._metric(cfg, "DatabaseConnections", "DBClusterIdentifier", ident)
            vessels.append({
                "name": ident, "unit": "ACU",
                "fill": clamp(acu / maxacu) if acu is not None else None,
                "used": acu, "ceiling": maxacu, "conns": conns,
                "status": c.get("Status", ""),
            })

        vessels.sort(key=lambda v: -(v["fill"] or 0))
        cap = int(cfg.get("max_tanks") or 8)
        return {"cistern": {"vessels": vessels[:cap], "total": len(vessels),
                            "region": cfg.get("region") or "us-east-1"}}

    # ---- render -----------------------------------------------------------
    def build(self):
        self.t = 0.0
        self.bubbles = {}   # tank index -> [[x, y], ...]

    def update(self, dt, frame, st):
        self.t += dt

    def _fill_color(self, fill):
        if fill is None:
            return C_BLUE
        if fill >= 0.85:
            return C_RED
        if fill >= 0.70:
            return C_YELLOW
        return C_CYAN

    def _draw_tank(self, scr, tx, top, tw, th, v, idx):
        fill = v["fill"]
        col = self._fill_color(fill)
        wall = cp(C_BLUE) | curses.A_DIM
        # walls + base
        for yy in range(top, top + th):
            putch(scr, yy, tx, "│", wall)
            putch(scr, yy, tx + tw - 1, "│", wall)
        for xx in range(tx, tx + tw):
            putch(scr, top + th - 1, xx, "─", wall)
        inner = tw - 2
        if inner < 1 or fill is None:
            if fill is None:
                put(scr, top + th // 2, tx + 1, "?".center(inner), cp(C_WHITE) | curses.A_DIM)
        else:
            water_rows = int(round(fill * (th - 1)))
            surface_y = top + th - 1 - water_rows
            for r in range(water_rows):
                yy = top + th - 2 - r
                if yy <= top:
                    break
                ch = "≈" if r == water_rows - 1 else "█"
                a = curses.A_BOLD if r == water_rows - 1 else curses.A_NORMAL
                # animated surface ripple
                if r == water_rows - 1:
                    for xx in range(tx + 1, tx + tw - 1):
                        phase = math.sin(self.t * 2 + xx * 0.6 + idx)
                        sc = "≈" if phase > 0 else "~"
                        putch(scr, yy, xx, sc, cp(col) | curses.A_BOLD)
                else:
                    put(scr, yy, tx + 1, ch * inner, cp(col) | a)
            # bubbles rise with connections
            conns = v.get("conns") or 0
            bs = self.bubbles.setdefault(idx, [])
            if conns and water_rows > 1 and random.random() < clamp(conns / 40.0, 0.05, 0.6):
                bs.append([random.randint(tx + 1, tx + tw - 2), top + th - 2])
            for b in bs:
                b[1] -= 0.5
                if b[1] > surface_y:
                    putch(scr, int(b[1]), b[0], "∘", cp(C_WHITE) | curses.A_DIM)
            self.bubbles[idx] = [b for b in bs if b[1] > surface_y and b[1] > top]
            # overflow warning at the brim
            if fill >= 0.85 and int(self.t * 3) % 2:
                put(scr, top, tx + 1, "⚠".center(inner), cp(C_RED) | curses.A_BOLD)

        # label + readout below the tank
        nm = v["name"]
        short = nm[:tw]
        ly = top + th
        put(scr, ly, tx, short.center(tw), cp(C_WHITE) | curses.A_BOLD)
        if fill is None:
            readout = "—"
            rc = C_WHITE
        else:
            readout = f"{fill * 100:.0f}%"
            rc = col
        put(scr, ly + 1, tx, readout.center(tw), cp(rc) | curses.A_BOLD)
        if v.get("used") is not None:
            det = f"{v['used']:.0f}/{v['ceiling']:.0f}{v['unit']}"
            put(scr, ly + 2, tx, det.center(tw)[:tw], cp(C_BLUE) | curses.A_DIM)

    def draw(self, scr, frame, st):
        c = st.get("cistern")
        if not c:
            center(scr, self.h // 2, "sounding the tanks…", cp(C_CYAN) | curses.A_DIM)
            return
        if c.get("error"):
            center(scr, self.h // 2, f"⚠ {c['error']}", cp(C_RED) | curses.A_BOLD)
            return
        vessels = c.get("vessels", [])
        if not vessels:
            center(scr, self.h // 2, "no databases found", cp(C_CYAN) | curses.A_DIM)
            return

        near = sum(1 for v in vessels if (v["fill"] or 0) >= 0.85)
        put(scr, 1, 3, f"RESERVOIRS · {len(vessels)} tanks", cp(C_WHITE) | curses.A_BOLD)
        if near:
            alert = f"⚠ {near} NEAR CAPACITY"
            blink = curses.A_BOLD if int(self.t * 3) % 2 else curses.A_DIM
            put(scr, 1, max(3, self.w - len(alert) - 4), alert, cp(C_RED) | blink)

        n = len(vessels)
        top = 3
        th = self.h - top - 5          # leave 3 rows for labels + base
        if th < 4:
            th = max(2, self.h - top - 2)
        gap = 2
        tw = int(clamp((self.w - 4 - gap * (n - 1)) / max(n, 1), 7, 18))
        total_w = n * tw + (n - 1) * gap
        x0 = max(2, (self.w - total_w) // 2)
        for i, v in enumerate(vessels):
            tx = x0 + i * (tw + gap)
            if tx + tw > self.w - 1:
                break
            self._draw_tank(scr, tx, top, tw, th, v, i)

        if c.get("total", 0) > n:
            put(scr, self.h - 2, 3, f"+{c['total'] - n} more…", cp(C_WHITE) | curses.A_DIM)

    def hud(self, st):
        c = st.get("cistern") or {}
        vessels = c.get("vessels", [])
        if not vessels:
            return [("rds", "…")]
        near = sum(1 for v in vessels if (v["fill"] or 0) >= 0.85)
        fullest = max(vessels, key=lambda v: v["fill"] or 0)
        return [("region", c.get("region", "—")),
                ("tanks", str(c.get("total", len(vessels)))),
                ("fullest", f"{(fullest['fill'] or 0) * 100:.0f}%"),
                ("near_cap", str(near))]
