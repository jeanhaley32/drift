#!/usr/bin/env python3
#
# drift — an ambient, themed "living terminal".
# Copyright (C) 2026 Jean Haley
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. This program is distributed WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
# the GNU General Public License for more details: <https://www.gnu.org/licenses/>.
"""
drift  —  an ambient, themed "living terminal".

Strongly-themed scenes cycle on random intervals; sprites wander in and
interact; and your machine's real telemetry (CPU, memory, battery, and a lot
of *network* detail) is the material the scenes are made of.

It's part screensaver, part generative art, part glanceable system monitor.

Themes in this build:
  COSMOS   retro space — stars, planets, meteors, a UFO and a rocket
  BOILER   steampunk machine room — gauges, gears, pistons, steam
  SIGNAL   live network node-graph — connections, packets, nearby Wi-Fi

Telemetry is DISPLAY ONLY. Nothing is ever sent anywhere. The two network
features that would reach outside the machine — public-IP lookup and per-process
remote-host inspection (lsof) — are OFF unless you pass --public-ip / --lsof.

macOS, pure standard-library Python 3 + a few cheap shell tools. No installs.

Controls:  q quit   space next scene   n/p next/prev theme   l lock theme
           h toggle telemetry HUD   f toggle fps meter   +/- speed
Run:  ./drift
"""

import argparse
import curses
import json
import locale
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

# ----------------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------------
def clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def norm_log(v, cap):
    """Map a non-negative magnitude to 0..1 on a log curve (nice for traffic)."""
    if v <= 0:
        return 0.0
    import math
    return clamp(math.log10(1 + v) / math.log10(1 + cap))


# Animations were tuned at this frame rate. We drive per-frame motion/spawns by
# a dt-scaled factor against this reference, so the visuals look identical at any
# real fps — just smoother. (See run_app + each scene's update.)
REF_FPS = 20.0


def run(cmd, timeout=4.0):
    """Run a shell command, return stdout text or None. Never raises."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return None


class GlobalKeyMonitor:
    """Counts the TIMING of system-wide key-down events to drive the typing
    meter while you work in other apps.

    PRIVACY BY DESIGN: the callback only does `count += 1`. It NEVER reads,
    stores, logs, or transmits *which* key was pressed — there is no content to
    leak (no passwords, no text). It also installs a LISTEN-ONLY event tap, so
    it cannot intercept, block, or modify your keystrokes. Requires macOS
    Input Monitoring permission for the host terminal; degrades to off without it.
    """

    def __init__(self):
        self.count = 0
        self._last = 0
        self.ok = False
        self.error = None
        self._tap = None
        self._Q = None

    def start(self):
        try:
            import Quartz
        except ImportError:
            self.error = "pyobjc Quartz not installed"
            return False
        self._Q = Quartz
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self):
        Q = self._Q

        def cb(proxy, etype, event, refcon):
            # Count only. The keycode in `event` is deliberately never inspected.
            if etype == Q.kCGEventKeyDown:
                self.count += 1
            elif etype == Q.kCGEventTapDisabledByTimeout and self._tap:
                Q.CGEventTapEnable(self._tap, True)
            return event

        try:
            mask = Q.CGEventMaskBit(Q.kCGEventKeyDown)
            tap = Q.CGEventTapCreate(
                Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                Q.kCGEventTapOptionListenOnly, mask, cb, None)
            if not tap:
                self.error = "no permission — grant Input Monitoring & restart"
                return
            self._tap = tap
            src = Q.CFMachPortCreateRunLoopSource(None, tap, 0)
            Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), src,
                                 Q.kCFRunLoopCommonModes)
            Q.CGEventTapEnable(tap, True)
            self.ok = True
            Q.CFRunLoopRun()
        except Exception as e:                       # never take down drift
            self.error = f"key monitor error: {e}"

    def pop(self):
        """Return key-downs since the last call (timing only)."""
        n = self.count - self._last
        self._last = self.count
        return n


# ----------------------------------------------------------------------------
# Stat parsers (pure functions, unit-testable)
# ----------------------------------------------------------------------------
def parse_netstat_ib(text):
    """Sum cumulative in/out bytes across physical interfaces (en*, utun*...).
    netstat -ib repeats each interface per address family with identical byte
    counters, so we keep the max per interface name to avoid double counting."""
    if not text:
        return None
    per = {}
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 7:
            continue
        name = f[0]
        if name == "lo0":
            continue
        # Leading columns vary (Link rows omit Address), but the tail is stable:
        #   ... Ibytes Opkts Oerrs Obytes Coll  ->  Ibytes=f[-5], Obytes=f[-2]
        try:
            ib = int(f[-5]); ob = int(f[-2])
        except (ValueError, IndexError):
            continue
        cur = per.get(name, (0, 0))
        per[name] = (max(cur[0], ib), max(cur[1], ob))
    if not per:
        return None
    return (sum(v[0] for v in per.values()), sum(v[1] for v in per.values()))


def parse_vm_stat(text, total_bytes):
    """Return used-memory fraction 0..1 from `vm_stat` output."""
    if not text or not total_bytes:
        return None
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    vals = {}
    for line in text.splitlines():
        mm = re.match(r'"?(.+?)"?:\s+(\d+)\.', line.strip())
        if mm:
            vals[mm.group(1).strip()] = int(mm.group(2))
    active = vals.get("Pages active", 0)
    wired = vals.get("Pages wired down", 0)
    comp = vals.get("Pages occupied by compressor", 0)
    used = (active + wired + comp) * page
    return clamp(used / total_bytes)


def parse_pmset(text):
    """Return (percent 0..100 or None, charging bool)."""
    if not text:
        return (None, False)
    m = re.search(r"(\d+)%", text)
    pct = int(m.group(1)) if m else None
    charging = ("AC Power" in text) or ("charging" in text and "discharging" not in text)
    return (pct, charging)


def parse_ping(text):
    """Return (rtt_ms or None, loss_bool)."""
    if not text:
        return (None, True)
    m = re.search(r"time[=<]([\d.]+)\s*ms", text)
    loss = "100.0% packet loss" in text or "100% packet loss" in text
    return (float(m.group(1)) if m else None, loss or m is None)


def parse_arp_count(text):
    if not text:
        return None
    n = 0
    for line in text.splitlines():
        if "(" in line and ")" in line and "incomplete" not in line:
            n += 1
    return n


def parse_conns(text):
    """Count ESTABLISHED tcp connections."""
    if not text:
        return None
    return sum(1 for ln in text.splitlines() if "ESTABLISHED" in ln)


def parse_ssid(text):
    """Current SSID from `networksetup -getairportnetwork`. On macOS 14+ this
    often reports 'not associated' even while online, so it's only a fallback —
    parse_wifi (system_profiler) is the primary source."""
    if not text:
        return None
    m = re.search(r"Current Wi-Fi Network:\s*(.+)", text)
    if m:
        return m.group(1).strip() or None
    return None


def _clean_ssid(name):
    """macOS 14+ redacts network NAMES to '<redacted>' unless the terminal has
    Location Services permission. Surface that as 'hidden' (we're connected, we
    just can't read the name) rather than a blank that looks like 'offline'."""
    if name in (None, ""):
        return None
    if name == "<redacted>":
        return "hidden"
    return name


def parse_wifi(text):
    """From `system_profiler SPAirPortDataType`, return
    (ssid, current_rssi, [(ssid, rssi), ...] for nearby networks).
    RSSI comes through even when names are redacted by the OS."""
    if not text:
        return (None, None, [])
    # current network name lives under "Current Network Information:" (macOS 14+
    # label; older builds used "Current Wi-Fi Network:")
    ssid = None
    cm = re.search(r"Current Network Information:\s*\n\s*(.+?):", text)
    if cm:
        ssid = _clean_ssid(cm.group(1).strip())
    # current link RSSI = the first Signal/Noise in the file (the current net)
    cur_rssi = None
    m = re.search(r"Signal / Noise:\s*(-?\d+)\s*dBm", text)
    if m:
        cur_rssi = int(m.group(1))
    # nearby networks block: lines "NAME:" then later "Signal / Noise: -xx dBm"
    neighbors = []
    block = text.split("Other Local Wi-Fi Networks:")
    if len(block) > 1:
        seg = block[1]
        cur_name = None
        for line in seg.splitlines():
            s = line.strip()
            mm = re.match(r"(.+):$", s)
            if mm and "PHY Mode" not in s and "Network Type" not in s:
                cur_name = mm.group(1).strip()
            rm = re.search(r"Signal / Noise:\s*(-?\d+)\s*dBm", s)
            if rm and cur_name:
                neighbors.append((_clean_ssid(cur_name) or "hidden",
                                  int(rm.group(1))))
                cur_name = None
    return (ssid, cur_rssi, neighbors[:12])


def parse_boottime(text):
    """sysctl -n kern.boottime -> uptime seconds."""
    if not text:
        return None
    m = re.search(r"sec\s*=\s*(\d+)", text)
    if not m:
        return None
    return max(0.0, time.time() - int(m.group(1)))


def parse_top_proc(text):
    """`ps -Aceo pcpu,comm -r` -> (name, pct) of the busiest process."""
    if not text:
        return (None, 0.0)
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return (None, 0.0)
    f = lines[1].split(None, 1)
    try:
        pct = float(f[0])
    except (ValueError, IndexError):
        return (None, 0.0)
    name = (f[1].strip() if len(f) > 1 else "?")
    return (os.path.basename(name), pct)


def rssi_to_frac(rssi):
    """Map Wi-Fi RSSI dBm (~ -30 great .. -90 awful) to 0..1."""
    if rssi is None:
        return 0.5
    return clamp((rssi + 90) / 60.0)


_UNIT = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
         "KB": 1000, "MB": 1000**2, "GB": 1000**3}


def _to_bytes(s):
    m = re.match(r"([\d.]+)\s*([KMGT]i?B|B)", s.strip())
    if not m:
        return 0
    return int(float(m.group(1)) * _UNIT.get(m.group(2), 1))


def parse_nettop(text):
    """Parse `nettop -n -l 1 -J bytes_in,bytes_out` (a process->connection tree)
    into {key: {proc, proto, local, remote, in, out}} of CUMULATIVE bytes.
    Diffing two snapshots over time yields per-connection rates."""
    out = {}
    proc = None
    for ln in (text or "").splitlines():
        if not ln.strip():
            continue
        if not ln[0].isspace():                      # process header row
            # name may contain single spaces; the byte columns are >=2 spaces
            # away, and the name ends with ".PID" which we strip.
            chunk = re.split(r"\s{2,}", ln.strip())[0]
            # ignore numeric/address-like top-level rows (not real process names)
            proc = "?" if re.fullmatch(r"[\d.]+", chunk) else (
                re.sub(r"\.\d+$", "", chunk) or chunk)
            continue
        # connection row, optionally followed by two byte columns
        m = re.match(
            r"\s+((?:tcp|udp)[46])\s+(\S+?)<->(\S+?)"
            r"(?:\s+([\d.]+\s*\w+)\s+([\d.]+\s*\w+))?\s*$", ln)
        if not m:
            continue
        proto, local, remote = m.group(1), m.group(2), m.group(3)
        ib = _to_bytes(m.group(4)) if m.group(4) else 0
        ob = _to_bytes(m.group(5)) if m.group(5) else 0
        out[f"{proto} {local}<->{remote}"] = {
            "proc": proc or "?", "proto": proto, "local": local,
            "remote": remote, "in": ib, "out": ob}
    return out


def remote_port(remote):
    m = re.search(r"[:.](\d+)$", remote)
    return int(m.group(1)) if m else None


# --- Docker ---------------------------------------------------------------
_DUNIT = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3,
          "TB": 1000**4, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
          "TiB": 1024**4}


def _docker_bytes(s):
    m = re.match(r"\s*([\d.]+)\s*([kKMGT]?i?B)\s*$", s)
    if not m:
        return 0.0
    return float(m.group(1)) * _DUNIT.get(m.group(2), 1)


def _pct(s):
    try:
        return float(s.strip().rstrip("%"))
    except ValueError:
        return 0.0


def parse_docker_stats(text):
    """Parse `docker stats --no-stream` formatted as
    Name|CPU%|MEM%|MemUsage|NetIO|BlockIO|PIDs into a list of dicts.
    NET/BLOCK byte counts are cumulative -> diff over time for rates."""
    out = []
    for ln in (text or "").splitlines():
        f = ln.split("|")
        if len(f) < 7 or not f[0].strip():
            continue
        mu = f[3].split("/")
        nio = f[4].split("/")
        bio = f[5].split("/")
        try:
            pids = int(f[6].strip())
        except ValueError:
            pids = 0
        out.append({
            "name": f[0].strip(),
            "cpu": clamp(_pct(f[1]) / 100.0),
            "mem": clamp(_pct(f[2]) / 100.0),
            "mem_bytes": _docker_bytes(mu[0]) if mu else 0.0,
            "net_in": _docker_bytes(nio[0]) if len(nio) > 0 else 0.0,
            "net_out": _docker_bytes(nio[1]) if len(nio) > 1 else 0.0,
            "blk_in": _docker_bytes(bio[0]) if len(bio) > 0 else 0.0,
            "blk_out": _docker_bytes(bio[1]) if len(bio) > 1 else 0.0,
            "pids": pids,
        })
    return out


def parse_docker_ps(text):
    """`docker ps -a --format {{.Names}}|{{.Image}}|{{.State}}|{{.Status}}`
    -> {name: {image, state, status}}."""
    d = {}
    for ln in (text or "").splitlines():
        f = ln.split("|")
        if len(f) < 4 or not f[0].strip():
            continue
        d[f[0].strip()] = {"image": f[1].strip(), "state": f[2].strip(),
                           "status": f[3].strip()}
    return d


# --- GitHub ---------------------------------------------------------------
def parse_contributions(cal):
    """From a GitHub contributionCalendar dict, return
    (grid, total, current_streak, longest_streak) where grid is a list of weeks,
    each a list of 7 intensity levels 0-4 (or -1 for days outside the range)."""
    weeks = cal.get("weeks", [])
    days = [(d["date"], d["contributionCount"])
            for w in weeks for d in w["contributionDays"]]
    counts = [c for _, c in days]
    maxc = max(counts) if counts else 0

    def lvl(c):
        if c <= 0:
            return 0
        if maxc <= 0:
            return 1
        return min(4, 1 + int(c / maxc * 3.999))

    grid = []
    for w in weeks:
        col = [-1] * 7
        for d in w["contributionDays"]:
            col[d["weekday"]] = lvl(d["contributionCount"])
        grid.append(col)
    longest = run = 0
    for _, c in days:
        run = run + 1 if c > 0 else 0
        longest = max(longest, run)
    cur = 0
    for _, c in reversed(days):
        if c > 0:
            cur += 1
        else:
            break
    total = cal.get("totalContributions", sum(counts))
    return grid, total, cur, longest


# ----------------------------------------------------------------------------
# Telemetry sampler (background thread; render loop never blocks on shell)
# ----------------------------------------------------------------------------
class Telemetry:
    def __init__(self, opts):
        self.opts = opts
        self.lock = threading.Lock()
        self.running = False
        self.ncpu = os.cpu_count() or 4
        self.total_mem = self._memsize()
        self.host = socket.gethostname().split(".")[0]
        try:
            self.user = os.getlogin()
        except OSError:
            self.user = os.environ.get("USER", "user")
        self.iface = self._primary_iface()
        # drift's location-authorized Wi-Fi component (un-redacts SSIDs on macOS
        # 14+). It's a tiny drift-owned .app — the only shape macOS lets a
        # terminal-launched program hold its own Location grant — that drift
        # builds, authorizes, and reads on its own. The prompt and the Location
        # Services entry both read "drift". Falls back to system_profiler
        # ("hidden" names) if it can't be built/authorized.
        here = os.path.dirname(os.path.abspath(__file__))
        self.wifi_dir = os.path.join(here, "wifi-helper")
        self.wifi_app = os.path.join(self.wifi_dir, "DriftWiFi.app")
        self.wifi_build = os.path.join(self.wifi_dir, "build.sh")
        self.wifi_out = os.path.join(tempfile.gettempdir(), "drift-wifi.json")
        self._wifi_built_tried = False   # build attempted this session?
        self._wifi_auth_tried = False    # auth prompt launched this session?
        self._wifi_ok = False            # have we ever read real, un-redacted data?
        # shared, normalized-ish data
        self.d = {
            "cpu": 0.0, "load": (0.0, 0.0, 0.0), "mem": 0.0, "disk": 0.0,
            "batt": None, "charging": False, "uptime": 0.0,
            "down_kbps": 0.0, "up_kbps": 0.0, "net": 0.0,
            "latency": None, "loss": False, "conns": 0, "lan": 0,
            "ssid": None, "rssi": None, "neighbors": [],
            "procs": 0, "topproc": None, "topcpu": 0.0,
            "pubip": None, "lan_ip": self._lan_ip(), "gateway": None,
            "vpn": False, "remotes": 0, "sockets": [],
            "docker": [], "docker_ok": False, "github": {"ok": False},
        }
        self._last_net = None  # (ibytes, obytes, t)
        self._ever_latency = False
        self._prev_nettop = None  # (dict, t)
        self._prev_docker = None  # (dict_by_name, t)
        # one-time, quick daemon probe (so rotation can include HARBOR or not)
        self.docker_ok = bool(run(["docker", "version", "--format",
                                   "{{.Server.Version}}"], 2))
        self.d["docker_ok"] = self.docker_ok
        # gh auth check is LOCAL (keyring, no network) -> fast & safe at startup
        try:
            self.gh_ok = subprocess.run(["gh", "auth", "status"],
                                        capture_output=True, timeout=4).returncode == 0
        except (OSError, subprocess.SubprocessError):
            self.gh_ok = False

    # --- one-time / helpers ---
    def _memsize(self):
        out = run(["sysctl", "-n", "hw.memsize"], 2)
        try:
            return int(out.strip())
        except (AttributeError, ValueError):
            return 0

    def _lan_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 1))   # TEST-NET, no traffic sent
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    def _primary_iface(self):
        out = run(["route", "-n", "get", "default"], 2)
        if out:
            m = re.search(r"interface:\s*(\S+)", out)
            if m:
                return m.group(1)
        return "en0"

    # --- threading ---
    def start(self):
        # Launch the sampler thread WITHOUT pre-sampling on the calling thread —
        # the slow tier (system_profiler/ping) takes seconds and would otherwise
        # block the UI from drawing its first frame. The thread's first loop
        # iteration samples all tiers; scenes render with defaults until then.
        self.running = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def stop(self):
        self.running = False

    def snapshot(self):
        with self.lock:
            return dict(self.d)

    def _set(self, **kw):
        with self.lock:
            self.d.update(kw)

    def _loop(self):
        last_med = last_slow = last_sock = last_dock = last_gh = 0.0
        while self.running:
            now = time.time()
            self._sample_fast()
            if now - last_sock >= 2.0:
                self._sample_sockets(); last_sock = now
            if self.docker_ok and now - last_dock >= 5.0:
                self._sample_docker(); last_dock = now
            # GitHub hits the network + rate limit, so refresh slowly (5 min)
            if self.gh_ok and now - last_gh >= 300.0:
                self._sample_github(); last_gh = now
            if now - last_med >= 4.0:
                self._sample_med(); last_med = now
            if now - last_slow >= 30.0:
                self._sample_slow(); last_slow = now
            time.sleep(1.0)

    # the fields are identical whether we query viewer{} or user(login:){}
    _GH_FIELDS = (
        "login followers{totalCount} following{totalCount} "
        "repositories(first:100,ownerAffiliations:OWNER){totalCount nodes{stargazerCount}} "
        "contributionsCollection{contributionCalendar{totalContributions "
        "weeks{contributionDays{contributionCount weekday date}}}}")

    def _sample_github(self):
        # Stats are scoped to a GitHub ACCOUNT (not a repo/org). Default is the
        # active gh account (viewer{} -> includes private contributions); with
        # --gh-user we query that specific user (public data only).
        target = getattr(self.opts, "gh_user", None)
        if target:
            root = "user"
            query = 'query{user(login:"%s"){%s}}' % (target, self._GH_FIELDS)
        else:
            root = "viewer"
            query = "query{viewer{%s}}" % self._GH_FIELDS
        out = run(["gh", "api", "graphql", "-f", "query=" + query], 15)
        try:
            v = json.loads(out)["data"][root]
        except (TypeError, ValueError, KeyError):
            self._set(github={"ok": False})
            return
        if v is None:                                   # bad/unknown login
            self._set(github={"ok": False, "bad_user": target})
            return
        cal = v["contributionsCollection"]["contributionCalendar"]
        login = v["login"]

        # Merge YOUR commits from specific (often private) repos into the
        # calendar, so the heatmap/streak/total reflect work GitHub doesn't
        # attribute to your graph (e.g. commits made under an unverified email).
        # Read-only: we only count commits authored by this account.
        private = 0
        repos = getattr(self.opts, "gh_repos", None) or []
        if repos:
            day = {d["date"]: d for w in cal["weeks"] for d in w["contributionDays"]}
            since = time.strftime("%Y-%m-%dT00:00:00Z",
                                  time.gmtime(time.time() - 366 * 86400))
            author = target or login
            for repo in repos:
                dates = run(["gh", "api", "--paginate",
                             f"repos/{repo}/commits?author={author}"
                             f"&since={since}&per_page=100",
                             "--jq", ".[].commit.author.date"], 25)
                if not dates:
                    continue
                for line in dates.splitlines():
                    private += 1
                    d = day.get(line[:10])              # YYYY-MM-DD bucket
                    if d:
                        d["contributionCount"] += 1
            # recompute the headline total to include the merged commits
            cal["totalContributions"] = sum(
                d["contributionCount"] for w in cal["weeks"]
                for d in w["contributionDays"])

        grid, total, cur, longest = parse_contributions(cal)
        stars = sum(n.get("stargazerCount", 0) for n in v["repositories"]["nodes"])

        def _int(s):
            s = (s or "").strip()
            return int(s) if s.lstrip("-").isdigit() else "?"
        author = target or "@me"
        prs = _int(run(["gh", "api",
                        f"search/issues?q=is:pr+is:open+author:{author}",
                        "--jq", ".total_count"], 10))
        # notifications are private to the authenticated viewer only
        notif = "—" if target else _int(
            run(["gh", "api", "notifications", "--jq", "length"], 10))
        rate = _int(run(["gh", "api", "rate_limit", "--jq", ".rate.remaining"], 8))
        self._set(github={
            "ok": True, "login": login,
            "followers": v["followers"]["totalCount"],
            "following": v["following"]["totalCount"],
            "repos": v["repositories"]["totalCount"], "stars": stars,
            "grid": grid, "total": total, "cur": cur, "longest": longest,
            "prs": prs, "notif": notif, "rate": rate,
            "private": private, "tracked": len(repos)})

    def _sample_docker(self):
        """Per-container telemetry. `docker stats` is the live source (cpu/mem/
        net/blk/pids); `docker ps -a` adds image + state. NET/BLK are cumulative
        so we diff for rates. All calls are timeout-guarded; a hung/stopped
        daemon just yields an empty harbor rather than freezing drift."""
        stats = parse_docker_stats(run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}|{{.MemUsage}}"
             "|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}"], 8))
        meta = parse_docker_ps(run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}|{{.Image}}|{{.State}}|{{.Status}}"], 4))
        now = time.time()
        prev, pt = self._prev_docker or ({}, now)
        dt = max(1.0, now - pt)
        live = {}
        conts = []
        for s in stats:
            n = s["name"]
            p = prev.get(n)
            s["net_bps"] = max(0.0, (s["net_in"] + s["net_out"] - p[0]) / dt) if p else 0.0
            s["blk_bps"] = max(0.0, (s["blk_in"] + s["blk_out"] - p[1]) / dt) if p else 0.0
            live[n] = (s["net_in"] + s["net_out"], s["blk_in"] + s["blk_out"])
            m = meta.get(n, {})
            s["image"] = m.get("image", "?")
            s["state"] = m.get("state", "running")
            s["status"] = m.get("status", "")
            conts.append(s)
        # include stopped containers (no stats row) as "docked"/sunk ships
        for n, m in meta.items():
            if n not in live and m.get("state") != "running":
                conts.append({"name": n, "cpu": 0.0, "mem": 0.0, "mem_bytes": 0.0,
                              "net_bps": 0.0, "blk_bps": 0.0, "pids": 0,
                              "image": m.get("image", "?"), "state": m.get("state", "exited"),
                              "status": m.get("status", "")})
        self._prev_docker = (live, now)
        self._set(docker=conts, docker_ok=bool(stats) or bool(meta))

    def _sample_sockets(self):
        """Per-connection traffic via nettop: diff cumulative byte counts between
        consecutive snapshots to get each socket's live in/out bytes-per-second."""
        cur = parse_nettop(run(["nettop", "-n", "-l", "1",
                                "-J", "bytes_in,bytes_out"], 4))
        now = time.time()
        socks = []
        if cur:
            prev_d, pt = self._prev_nettop or ({}, now)
            dt = max(0.5, now - pt)
            for key, c in cur.items():
                if "*" in c["remote"]:        # skip listeners / wildcards
                    continue
                p = prev_d.get(key)
                di = max(0.0, (c["in"] - p["in"]) / dt) if p else 0.0
                do = max(0.0, (c["out"] - p["out"]) / dt) if p else 0.0
                socks.append({**c, "in_bps": di, "out_bps": do})
            self._prev_nettop = (cur, now)
            socks.sort(key=lambda s: -(s["in_bps"] + s["out_bps"]))
            self._set(sockets=socks[:40])

    # --- sampling tiers ---
    def _sample_fast(self):
        try:
            load = os.getloadavg()
        except (OSError, AttributeError):
            load = (0.0, 0.0, 0.0)
        cpu = clamp(load[0] / self.ncpu)
        # throughput
        ib = parse_netstat_ib(run(["netstat", "-ib"], 3))
        down = up = 0.0
        now = time.time()
        if ib and self._last_net:
            pib, pob, pt = self._last_net
            dt = max(0.001, now - pt)
            down = max(0.0, (ib[0] - pib) / dt / 1024.0)
            up = max(0.0, (ib[1] - pob) / dt / 1024.0)
        if ib:
            self._last_net = (ib[0], ib[1], now)
        net = max(norm_log(down, 20000), norm_log(up, 5000))
        self._set(load=load, cpu=cpu, down_kbps=down, up_kbps=up, net=net)

    def _sample_med(self):
        mem = parse_vm_stat(run(["vm_stat"], 3), self.total_mem)
        try:
            st = os.statvfs("/")
            disk = clamp(1 - (st.f_bavail / st.f_blocks)) if st.f_blocks else 0.0
        except OSError:
            disk = 0.0
        batt, charging = parse_pmset(run(["pmset", "-g", "batt"], 3))
        upt = parse_boottime(run(["sysctl", "-n", "kern.boottime"], 2))
        conns = parse_conns(run(["netstat", "-an", "-p", "tcp"], 3))
        # -n = numeric: skip reverse-DNS on every neighbor (which can hang)
        lan = parse_arp_count(run(["arp", "-an"], 3))
        procs_out = run(["ps", "-A"], 3)
        procs = (len(procs_out.splitlines()) - 1) if procs_out else 0
        topname, topcpu = parse_top_proc(run(["ps", "-Aceo", "pcpu,comm", "-r"], 3))
        lat = loss = None
        gw = self.d.get("gateway")
        if gw:
            lat, loss = parse_ping(run(["ping", "-c", "1", "-t", "1", gw], 2))
            if lat is not None:
                self._ever_latency = True
        upd = dict(disk=disk, batt=batt, charging=charging,
                   conns=conns or 0, lan=lan or 0, procs=procs,
                   topproc=topname, topcpu=topcpu)
        if mem is not None:
            upd["mem"] = mem
        if upt is not None:
            upd["uptime"] = upt
        if gw:
            upd["latency"] = lat
            # only flag loss once we've actually heard from the gateway —
            # a gateway that simply never answers ICMP isn't "packet loss"
            upd["loss"] = bool(loss) and self._ever_latency
        self._set(**upd)

    def _wifi_build(self):
        """Build drift's Wi-Fi component on first need, if swiftc is available.
        Quiet no-op when already built or when Xcode tools are missing (we just
        fall back to system_profiler then)."""
        if self._wifi_built_tried or os.path.isdir(self.wifi_app):
            return
        self._wifi_built_tried = True
        if not (os.path.exists(self.wifi_build) and
                run(["which", "swiftc"], 3)):
            return
        run(["bash", self.wifi_build], 60)

    def _wifi_helper(self):
        """Read the JSON the Wi-Fi component last wrote, then kick a fresh
        refresh. The component must run via LaunchServices (`open`) to be its own
        location-authorized app; the read is decoupled from the launch so the
        sampler never blocks on it.

        First session-launch uses `--auth` (foreground) so macOS shows the
        "drift" location prompt once; after we've seen real data — or after that
        one attempt — we use background `-g` reads (no prompt, no focus steal)."""
        self._wifi_build()
        if not os.path.isdir(self.wifi_app):
            return None
        data = None
        try:
            with open(self.wifi_out) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if data and (data.get("ssid") or data.get("neighbors")):
            self._wifi_ok = True          # real, un-redacted data has arrived
        if not self._wifi_ok and not self._wifi_auth_tried:
            # one-time: trigger the macOS Location prompt for "drift"
            self._wifi_auth_tried = True
            run(["open", "-n", self.wifi_app, "--args",
                 "--auth", "--scan", "--out", self.wifi_out], 4)
        else:
            run(["open", "-g", "-n", self.wifi_app, "--args",
                 "--scan", "--out", self.wifi_out], 4)
        return data

    def _sample_slow(self):
        # gateway
        gout = run(["route", "-n", "get", "default"], 2)
        gw = None
        if gout:
            m = re.search(r"gateway:\s*(\S+)", gout)
            if m:
                gw = m.group(1)
        # wifi — prefer the location-authorized helper (real, un-redacted names
        # on macOS 14+). Falls back to system_profiler (signal strength + "hidden"
        # names) when the helper isn't built/authorized.
        ssid = rssi = None
        neighbors = []
        # Use the helper if it returned real (un-redacted) data. The "auth" field
        # is only reliable in --auth mode, so we trust the payload instead: a
        # real ssid or named neighbors means CoreWLAN was authorized.
        hw = self._wifi_helper()
        if hw and hw.get("ok") and (hw.get("ssid") or hw.get("neighbors")):
            ssid = hw.get("ssid") or None
            rssi = hw.get("rssi")
            neighbors = [(n[0], n[1]) for n in hw.get("neighbors", [])
                         if isinstance(n, list) and len(n) == 2 and n[0]]
        if ssid is None and rssi is None and not neighbors:
            ssid, rssi, neighbors = parse_wifi(
                run(["system_profiler", "SPAirPortDataType"], 8))
            if not ssid:
                ssid = parse_ssid(
                    run(["networksetup", "-getairportnetwork", self.iface], 3))
        # vpn (utun interface with an inet addr, beyond loopback)
        vpn = False
        ifc = run(["ifconfig"], 3) or ""
        for blk in re.split(r"\n(?=\w)", ifc):
            if blk.startswith("utun") and "inet " in blk:
                vpn = True
        upd = dict(gateway=gw, ssid=ssid, rssi=rssi, neighbors=neighbors, vpn=vpn)
        if self.opts.public_ip:
            pip = run(["dig", "+short", "myip.opendns.com",
                       "@resolver1.opendns.com"], 3)
            upd["pubip"] = pip.strip() if pip else None
        if self.opts.lsof:
            lo = run(["lsof", "-nP", "-i", "-sTCP:ESTABLISHED"], 4)
            remotes = set()
            if lo:
                for ln in lo.splitlines()[1:]:
                    mm = re.search(r"->([\d.]+):\d+", ln)
                    if mm:
                        remotes.add(mm.group(1))
            upd["remotes"] = len(remotes)
        self._set(**upd)


# ----------------------------------------------------------------------------
# Crash-safe curses drawing
# ----------------------------------------------------------------------------
C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE, C_BLUE = 1, 2, 3, 4, 5, 6, 7
_HAS_COLOR = False


def init_colors():
    global _HAS_COLOR
    if not curses.has_colors():
        _HAS_COLOR = False
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    for pair, fg in ((C_RED, curses.COLOR_RED), (C_GREEN, curses.COLOR_GREEN),
                     (C_YELLOW, curses.COLOR_YELLOW), (C_CYAN, curses.COLOR_CYAN),
                     (C_MAGENTA, curses.COLOR_MAGENTA), (C_WHITE, curses.COLOR_WHITE),
                     (C_BLUE, curses.COLOR_BLUE)):
        try:
            curses.init_pair(pair, fg, bg)
        except curses.error:
            pass
    _HAS_COLOR = True


def cp(n):
    return curses.color_pair(n) if _HAS_COLOR else curses.A_NORMAL


def put(scr, y, x, text, attr=0):
    h, w = scr.getmaxyx()
    y = int(y); x = int(x)
    if y < 0 or y >= h or x >= w:
        return
    if x < 0:
        text = text[-x:]; x = 0
    if not text:
        return
    text = text[: w - x]
    if y == h - 1 and x + len(text) >= w:
        text = text[: w - x - 1]
        if not text:
            return
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


def putch(scr, y, x, ch, attr=0):
    h, w = scr.getmaxyx()
    y = int(y); x = int(x)
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    if y == h - 1 and x == w - 1:
        return
    try:
        scr.addch(y, x, ch, attr)
    except curses.error:
        pass


def center(scr, y, text, attr=0):
    _, w = scr.getmaxyx()
    put(scr, y, max(0, (w - len(text)) // 2), text, attr)


def plot_line(scr, y0, x0, y1, x1, ch, attr=0):
    """Bresenham line of single chars."""
    y0, x0, y1, x1 = int(y0), int(x0), int(y1), int(x1)
    dx = abs(x1 - x0); dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    guard = 0
    while guard < 2000:
        guard += 1
        putch(scr, y0, x0, ch, attr)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy; x0 += sx
        if e2 <= dx:
            err += dx; y0 += sy


# ----------------------------------------------------------------------------
# Scene base
# ----------------------------------------------------------------------------
class Scene:
    """Base class for a driftboard. Subclasses render with build/update/draw/hud
    and may optionally pull their own data via fetch() on a declared interval.

    Board contract (all optional, sensible defaults):
      name, title              identity (name is the manifest `type`)
      CONFIG = {key: {...}}     per-instance config schema (validated + defaulted)
      SECRETS = ["ENV_VAR"]     required env vars (checked by default available())
      interval = None           seconds between fetch() calls (None = no fetch)
      available(tele, cfg)      whether this board can run right now
      fetch(cfg) -> dict        background data pull; merged into the render state
    """
    name = "scene"
    title = "SCENE"
    CONFIG = {}
    SECRETS = []
    interval = None

    def __init__(self, h, w, tele, cfg=None):
        self.tele = tele
        self.cfg = cfg or {}
        self.h = self.w = 0
        self.resize(h, w)

    @classmethod
    def available(cls, tele, cfg=None):
        """Default: available unless a required secret env var is missing."""
        return all(os.environ.get(k) for k in cls.SECRETS)

    def fetch(self, cfg):
        """Background data pull (runs off the render thread). Return a dict that
        gets merged into the render state under this board's namespace. Default
        no-op for boards that only consume shared telemetry."""
        return {}

    def resize(self, h, w):
        self.h, self.w = h, w
        self.build()

    def build(self):
        pass

    def update(self, dt, frame, st):
        pass

    def draw(self, scr, frame, st):
        pass

    def hud(self, st):
        """Return list of (label, value) for the decoder HUD."""
        return []


# --- board registry ---------------------------------------------------------
# Boards self-register here via @board; manifests reference a board by `name`.
# Built-in boards (this file) and plugin boards (driftboards/*.py) share it.
BOARDS = {}


def board(cls):
    """Class decorator: register a Board subclass by its `name`."""
    BOARDS[cls.name] = cls
    return cls


# --- shared starfield ----------------------------------------------------
def make_stars(h, w, n):
    stars = []
    for _ in range(n):
        stars.append([random.randint(1, max(1, h - 2)),
                      random.randint(1, max(1, w - 2)),
                      random.randint(0, 7),
                      random.random() < 0.25])
    return stars


def draw_stars(scr, stars, frame, color=C_WHITE):
    tw = ".:*+:."
    for (y, x, ph, bright) in stars:
        ch = tw[(frame // 5 + ph) % len(tw)]
        attr = cp(color) if bright else (cp(C_BLUE) | curses.A_DIM)
        putch(scr, y, x, ch, attr)


# ============================================================================
# THEME 1 : COSMOS
# ============================================================================
PLANET_ART = ["  .--.  ", "-( ~~ )-", "  '--'  "]
ROCKET_ART = [" /\\ ", "|=*|", "|MB|", "/__\\"]
UFO_ART = [" .-=-. ", "(o-o-o)"]


class CosmosScene(Scene):
    name = "cosmos"
    title = "C O S M O S"

    def build(self):
        self.stars = make_stars(self.h, self.w, max(10, self.h * self.w // 90))
        self.meteors = []
        self.ufo_x = self.w + 5
        self.planet = (random.randint(3, max(3, self.h - 6)),
                       random.randint(self.w // 2, max(self.w // 2, self.w - 10)))
        self.pulse_t = 0.0
        self.rings = []
        self.rocket_y = float(self.h)

    def update(self, dt, frame, st):
        f = dt * REF_FPS                      # fps-independent frame factor
        # meteors spawn with download rate
        rate = 0.02 + st["net"] * 0.9
        if random.random() < rate * f:
            self.meteors.append([random.uniform(0, self.w), 0.0,
                                 random.uniform(0.6, 1.4) + st["net"]])
        for m in self.meteors:
            m[1] += m[2] * 1.6 * f
            m[0] += m[2] * 1.2 * f
        self.meteors = [m for m in self.meteors if m[1] < self.h and m[0] < self.w]
        # latency pulse beacon: ring period scales with latency
        lat = st.get("latency") or 60
        period = clamp(lat / 200.0, 0.15, 1.2) * 2.2
        self.pulse_t += dt
        if self.pulse_t >= period:
            self.pulse_t = 0.0
            self.rings.append(0.0)
        self.rings = [r + dt * 14 for r in self.rings]
        self.rings = [r for r in self.rings if r < max(self.h, self.w)]
        # rocket climbs faster under CPU load
        self.rocket_y -= (0.15 + st["cpu"] * 1.4) * f
        if self.rocket_y < -5:
            self.rocket_y = float(self.h + 4)
        # ufo drifts
        self.ufo_x -= 0.35 * f
        if self.ufo_x < -8:
            self.ufo_x = self.w + random.randint(4, 30)

    def draw(self, scr, frame, st):
        draw_stars(scr, self.stars, frame)
        # nearby wifi as a labeled star cluster (brightness = signal)
        nb = st.get("neighbors") or []
        for i, (ssid, rssi) in enumerate(nb[:8]):
            y = 2 + i
            x = 3
            b = rssi_to_frac(rssi)
            ch = "*" if b > 0.6 else ("+" if b > 0.35 else ".")
            col = C_YELLOW if b > 0.6 else C_CYAN
            put(scr, y, x, f"{ch} {ssid[:16]}", cp(col) | (curses.A_BOLD if b > 0.6 else curses.A_DIM))
        # latency pulse rings around a beacon
        by, bx = self.h // 2, self.w // 2
        for r in self.rings:
            steps = max(8, int(r * 4))
            import math
            for k in range(steps):
                a = 2 * math.pi * k / steps
                yy = by + r * 0.5 * math.sin(a)
                xx = bx + r * math.cos(a)
                putch(scr, yy, xx, "·", cp(C_CYAN) | curses.A_DIM)
        putch(scr, by, bx, "o", cp(C_CYAN) | curses.A_BOLD)
        # planet
        py, px = self.planet
        for i, ln in enumerate(PLANET_ART):
            put(scr, py + i, px, ln, cp(C_MAGENTA) | curses.A_BOLD)
        # meteors with little trails
        for m in self.meteors:
            putch(scr, m[1], m[0], "@", cp(C_YELLOW) | curses.A_BOLD)
            putch(scr, m[1] - 1, m[0] - 1, "\\", cp(C_RED))
        # rocket + exhaust
        rx = self.w - 8
        for i, ln in enumerate(ROCKET_ART):
            put(scr, self.rocket_y + i, rx, ln, cp(C_WHITE) | curses.A_BOLD)
        flame = ["\\/", "}{", "/\\"][frame % 3]
        put(scr, self.rocket_y + 4, rx + 1, flame, cp(C_RED) | curses.A_BOLD)
        # ufo
        for i, ln in enumerate(UFO_ART):
            put(scr, 2 + i, self.ufo_x, ln, cp(C_GREEN) | curses.A_BOLD)

    def hud(self, st):
        d = st["down_kbps"]; u = st["up_kbps"]
        return [("meteors = net down", f"{d:,.0f} KB/s"),
                ("pulse = latency", f"{st['latency']:.0f} ms" if st['latency'] else "—"),
                ("rocket = cpu", f"{st['cpu']*100:.0f}%"),
                ("stars(L) = nearby wifi", f"{len(st.get('neighbors') or [])} nets")]


# ============================================================================
# THEME 2 : BOILER ROOM
# ============================================================================
class BoilerScene(Scene):
    name = "boiler"
    title = "B O I L E R   R O O M"

    def build(self):
        self.steam = []
        self.gear_phase = 0.0
        self.piston = 0.0
        self.spike_flash = 0.0
        self.prev_cpu = 0.0

    def _gauge(self, scr, cy, cx, label, frac, color):
        """A little semicircular pressure dial with a needle."""
        import math
        frac = clamp(frac)
        put(scr, cy + 2, cx - 3, f"[{label}]", cp(C_WHITE) | curses.A_BOLD)
        # arc
        R = 5
        for k in range(0, 19):
            a = math.pi * (1 - k / 18.0)   # pi..0 (left to right, top arc)
            yy = cy - R * 0.5 * math.sin(a)
            xx = cx + R * math.cos(a)
            tick = "."
            if k in (0, 9, 18):
                tick = "|"
            col = C_GREEN if k < 11 else (C_YELLOW if k < 15 else C_RED)
            putch(scr, yy, xx, tick, cp(col))
        # needle
        a = math.pi * (1 - frac)
        ny = cy - (R - 1) * 0.5 * math.sin(a)
        nx = cx + (R - 1) * math.cos(a)
        plot_line(scr, cy, cx, ny, nx, "/", cp(color) | curses.A_BOLD)
        putch(scr, cy, cx, "O", cp(C_YELLOW) | curses.A_BOLD)
        pct = int(frac * 100)
        put(scr, cy + 3, cx - 2, f"{pct:3d}%", cp(color) | curses.A_BOLD)

    def update(self, dt, frame, st):
        f = dt * REF_FPS
        self.gear_phase += dt * (1 + st["cpu"] * 8)
        self.piston += dt * (2 + st["cpu"] * 10)
        # steam puffs when memory high
        if random.random() < (0.05 + st["mem"] * 0.5) * f:
            self.steam.append([self.h - 6, random.randint(4, max(5, self.w - 4)),
                               random.uniform(0.3, 0.8), random.choice("░▒.oO")])
        for s in self.steam:
            s[0] -= s[2] * f
        self.steam = [s for s in self.steam if s[0] > 1]
        # CPU spike -> relief valve flash
        if st["cpu"] - self.prev_cpu > 0.18 or st["cpu"] > 0.9:
            self.spike_flash = 1.0
        self.prev_cpu = st["cpu"]
        self.spike_flash = max(0.0, self.spike_flash - dt * 1.5)

    def _gear(self, scr, cy, cx, phase, color):
        import math
        teeth = 8
        for k in range(teeth):
            a = phase + 2 * math.pi * k / teeth
            yy = cy + 2 * 0.5 * math.sin(a) * 2
            xx = cx + 2 * math.cos(a)
            putch(scr, yy, xx, "*", cp(color) | curses.A_BOLD)
        putch(scr, cy, cx, "+", cp(color))

    def draw(self, scr, frame, st):
        # pipes / frame
        for x in range(2, self.w - 2):
            putch(scr, self.h - 4, x, "=", cp(C_YELLOW) | curses.A_DIM)
        # gauges row
        row = max(5, self.h // 3)
        thirds = self.w // 4
        self._gauge(scr, row, thirds, "CPU", st["cpu"], C_RED)
        self._gauge(scr, row, thirds * 2, "MEM", st["mem"], C_CYAN)
        self._gauge(scr, row, thirds * 3, "NET", st["net"], C_GREEN)
        # gears (speed = cpu)
        self._gear(scr, self.h - 8, 8, self.gear_phase, C_YELLOW)
        self._gear(scr, self.h - 9, 15, -self.gear_phase * 1.3, C_WHITE)
        # pistons (rate = cpu)
        import math
        for i in range(3):
            x = self.w - 10 + i * 3
            off = int(2 + 1.5 * math.sin(self.piston + i))
            for yy in range(self.h - 10, self.h - 10 + off):
                putch(scr, yy, x, "|", cp(C_WHITE))
            putch(scr, self.h - 10 + off, x, "#", cp(C_YELLOW) | curses.A_BOLD)
        # steam
        for s in self.steam:
            putch(scr, s[0], s[1], s[3], cp(C_WHITE) | curses.A_DIM)
        # relief flash on spike
        if self.spike_flash > 0.3:
            center(scr, row - 3, " ! OVER-PRESSURE !  PSSSST ",
                   cp(C_RED) | curses.A_BOLD | curses.A_REVERSE)
        # nameplate w/ top process
        tp = st.get("topproc")
        if tp:
            put(scr, self.h - 3, 4, f"~ throughput hog: {tp} ({st['topcpu']:.0f}%) ~",
                cp(C_YELLOW) | curses.A_DIM)

    def hud(self, st):
        return [("CPU gauge", f"{st['cpu']*100:.0f}%"),
                ("MEM gauge", f"{st['mem']*100:.0f}%"),
                ("NET gauge", f"{st['net']*100:.0f}%"),
                ("steam = mem, spike = cpu jump", "")]


# ============================================================================
# THEME 3 : SIGNAL  (live network node-graph)
# ============================================================================
class SignalScene(Scene):
    name = "signal"
    title = "S I G N A L"

    """A live Wi-Fi neighborhood radar. The network you're connected to sits at
    the hub; every nearby network is a spoke whose distance from the hub tracks
    its signal strength (stronger = closer), labeled with its real name. A radar
    sweep rotates around and pings each node as it passes."""

    def build(self):
        self.stars = make_stars(self.h, self.w, max(8, self.h * self.w // 130))
        self.pulse = []          # AP-broadcast rings from the hub
        self.pulse_t = 0.0
        self.sweep = 0.0         # radar sweep angle (radians)
        self.blips = []          # [node_index, t 0..1] signal motes drifting in
        self.layout = (self.h // 2, self.w // 2, [])

    def _layout(self, neighbors):
        """Place each neighbor around an ellipse; radius shrinks with signal
        strength so strong networks sit closer to the hub. Returns
        (cy, cx, [(y, x, angle, name, rssi, frac), ...])."""
        import math
        cy, cx = self.h // 2, self.w // 2
        n = max(1, len(neighbors))
        ry_far, rx_far = self.h * 0.40, self.w * 0.42
        ry_near, rx_near = self.h * 0.17, self.w * 0.17
        pts = []
        for k, (name, rssi) in enumerate(neighbors):
            a = 2 * math.pi * k / n - math.pi / 2
            frac = rssi_to_frac(rssi)                 # 0 weak .. 1 strong
            ry = lerp(ry_far, ry_near, frac)
            rx = lerp(rx_far, rx_near, frac)
            pts.append((cy + ry * math.sin(a), cx + rx * math.cos(a),
                        a, name, rssi, frac))
        return cy, cx, pts

    def update(self, dt, frame, st):
        import math
        nb = (st.get("neighbors") or [])[:12]
        self.layout = self._layout(nb)
        cy, cx, pts = self.layout
        # radar sweep rotates steadily
        self.sweep = (self.sweep + dt * 1.0) % (2 * math.pi)
        # hub broadcast rings, faster when our own link is strong
        frac_me = rssi_to_frac(st.get("rssi"))
        period = lerp(2.0, 0.8, frac_me)
        self.pulse_t += dt
        if self.pulse_t >= period:
            self.pulse_t = 0.0
            self.pulse.append(0.0)
        self.pulse = [r + dt * 14 for r in self.pulse if r < max(self.h, self.w)]
        # each node drifts signal motes inward at a rate set by its strength
        for k, (py, px, a, name, rssi, frac) in enumerate(pts):
            if random.random() < (0.04 + frac * 0.5) * dt * REF_FPS:
                self.blips.append([k, 0.0, 0.7 + frac * 1.6])
        for b in self.blips:
            b[1] += dt * b[2]
        self.blips = [b for b in self.blips if b[1] < 1.0 and b[0] < len(pts)]

    def draw(self, scr, frame, st):
        import math
        draw_stars(scr, self.stars, frame, C_BLUE)
        cy, cx, pts = self.layout
        # hub broadcast rings
        for r in self.pulse:
            steps = max(10, int(r * 4))
            for k in range(steps):
                a = 2 * math.pi * k / steps
                putch(scr, cy + r * 0.5 * math.sin(a), cx + r * math.cos(a),
                      "·", cp(C_BLUE) | curses.A_DIM)
        # radar sweep line
        ey = cy + (self.h * 0.46) * math.sin(self.sweep)
        ex = cx + (self.w * 0.48) * math.cos(self.sweep)
        plot_line(scr, cy, cx, ey, ex, "·", cp(C_GREEN) | curses.A_DIM)
        # spokes, nodes + labels
        for k, (py, px, a, name, rssi, frac) in enumerate(pts):
            # angular distance from the sweep -> recently-pinged nodes glow
            d = abs(((a - self.sweep + math.pi) % (2 * math.pi)) - math.pi)
            lit = d < 0.28
            col = C_GREEN if frac > 0.6 else (C_CYAN if frac > 0.33 else C_BLUE)
            plot_line(scr, cy, cx, py, px, "·",
                      cp(col) | (curses.A_DIM if not lit else 0))
            glyph = "◉" if frac > 0.6 else ("○" if frac > 0.33 else "∘")
            attr = cp(col) | (curses.A_BOLD if (lit or frac > 0.6) else curses.A_DIM)
            putch(scr, py, px, glyph, attr)
            # label: name on the outward side so it doesn't cross the hub
            text = f"{name[:18]} {rssi}"
            lx = int(px + 2) if px >= cx else int(px - 1 - len(text))
            put(scr, int(py), lx, text,
                cp(C_WHITE) | (curses.A_BOLD if lit else curses.A_DIM))
        # signal motes drifting inward along spokes
        for (k, t, sp) in self.blips:
            py, px, a, name, rssi, frac = pts[k]
            yy = lerp(py, cy, t); xx = lerp(px, cx, t)
            putch(scr, yy, xx, "•", cp(C_GREEN) | curses.A_BOLD)
        # central hub = the network we're connected to
        ssid = st.get("ssid")
        if ssid and ssid != "hidden":
            hub = f"▶ {ssid[:24]} ◀"
            hcol = C_MAGENTA
        elif ssid == "hidden":
            hub = "▶ (name hidden) ◀"
            hcol = C_YELLOW
        else:
            hub = "▶ not connected ◀"
            hcol = C_RED
        put(scr, cy, cx - len(hub) // 2, hub,
            cp(hcol) | curses.A_BOLD | curses.A_REVERSE)
        # our signal strength bar + host/ip beneath the hub
        rssi_me = st.get("rssi")
        bar_n = int(rssi_to_frac(rssi_me) * 10)
        bar = "█" * bar_n + "░" * (10 - bar_n)
        meta = f"{bar}  {rssi_me} dBm" if rssi_me is not None else "signal —"
        put(scr, cy + 1, cx - len(meta) // 2, meta, cp(C_GREEN) | curses.A_DIM)
        host = f"{self.tele.host} · {self.tele.d['lan_ip']}"
        put(scr, cy + 2, cx - len(host) // 2, host, cp(C_CYAN) | curses.A_DIM)
        # status line
        n_near = len(st.get("neighbors") or [])
        vpn = "  VPN" if st.get("vpn") else ""
        loss = "  !LOSS" if st.get("loss") else ""
        put(scr, self.h - 2, 3,
            f"connected: {ssid or '—'}   ·   {n_near} networks nearby   "
            f"·   LAN {st.get('lan', 0)} dev{vpn}{loss}",
            cp(C_GREEN) | curses.A_BOLD)
        if st.get("pubip"):
            put(scr, self.h - 3, 3, f"public {st['pubip']}", cp(C_YELLOW) | curses.A_DIM)

    def hud(self, st):
        rssi_me = st.get("rssi")
        return [("hub = connected network", (st.get("ssid") or "—")[:18]),
                ("our signal", f"{rssi_me} dBm" if rssi_me is not None else "—"),
                ("spokes = nearby wifi", f"{len(st.get('neighbors') or [])}"),
                ("closer spoke = stronger", "by RSSI"),
                ("sweep pings each node", "")]


# ============================================================================
# THEME 4 : SOCKETS  (a live patch-bay of individual connections)
# ============================================================================
PORT_COLOR = {443: C_GREEN, 80: C_YELLOW, 53: C_CYAN, 5353: C_CYAN,
              22: C_MAGENTA, 5223: C_BLUE}


class SocketsScene(Scene):
    """One row per real socket; each row's flowing glyphs are driven by THAT
    connection's actual download/upload bytes-per-second (via nettop)."""
    name = "sockets"
    title = "S O C K E T S"

    def build(self):
        self.phase = {}     # per-connection animation phase

    def update(self, dt, frame, st):
        for s in st.get("sockets", []):
            k = s["proto"] + s["local"] + s["remote"]
            rate = norm_log((s["in_bps"] + s["out_bps"]) / 1024.0, 4000)
            self.phase[k] = self.phase.get(k, 0.0) + dt * (2 + rate * 26)

    def _flow(self, scr, y, x, wd, rate, glyph, scroll, color):
        """A scrolling stream of glyphs; denser+brighter at higher rate."""
        if wd <= 0 or rate <= 0.001:
            return
        gap = max(2, int(9 * (1 - rate)) + 1)        # busy -> smaller gap
        attr = cp(color) | (curses.A_BOLD if rate > 0.25 else curses.A_DIM)
        for i in range(wd):
            if (i + int(scroll)) % gap == 0:
                putch(scr, y, x + i, glyph, attr)

    def draw(self, scr, frame, st):
        socks = st.get("sockets", [])
        center(scr, 2, "SOCKETS — live per-connection traffic",
               cp(C_GREEN) | curses.A_BOLD)
        host = self.tele.host
        tot_in = sum(s["in_bps"] for s in socks) / 1024.0
        tot_out = sum(s["out_bps"] for s in socks) / 1024.0
        put(scr, 3, 3, f"{host}   active sockets: {len(socks)}   "
                       f"Σ ↓{tot_in:,.0f}  ↑{tot_out:,.0f} KB/s",
            cp(C_CYAN) | curses.A_DIM)
        if not socks:
            center(scr, self.h // 2, "…gathering socket data (needs nettop)…",
                   cp(C_YELLOW) | curses.A_DIM)
            return
        # column layout (degrades on narrow terminals)
        x_proc, x_rem = 3, 16
        x_lane = 40
        lane_w = max(4, (self.w - x_lane - 24) // 2)
        x_up = x_lane + lane_w + 3
        x_num = x_up + lane_w + 2
        rows = min(len(socks), self.h - 6)
        for r in range(rows):
            s = socks[r]
            y = 5 + r
            k = s["proto"] + s["local"] + s["remote"]
            ph = self.phase.get(k, 0.0)
            port = remote_port(s["remote"])
            col = PORT_COLOR.get(port, C_WHITE)
            din = norm_log(s["in_bps"] / 1024.0, 4000)
            dout = norm_log(s["out_bps"] / 1024.0, 4000)
            active = (s["in_bps"] + s["out_bps"]) > 1
            pa = (cp(col) | curses.A_BOLD) if active else (cp(C_WHITE) | curses.A_DIM)
            put(scr, y, x_proc, s["proc"][:12], pa)
            put(scr, y, x_rem, s["remote"][:22], cp(C_WHITE) | (curses.A_DIM if not active else 0))
            putch(scr, y, x_lane - 1, "<", cp(C_GREEN) | curses.A_DIM)   # download dir
            self._flow(scr, y, x_lane, lane_w, din, "‹", -ph, C_GREEN)
            putch(scr, y, x_up - 1, ">", cp(C_YELLOW) | curses.A_DIM)    # upload dir
            self._flow(scr, y, x_up, lane_w, dout, "›", ph, C_YELLOW)
            ib = s["in_bps"] / 1024.0
            ob = s["out_bps"] / 1024.0
            num = f"↓{ib:5.0f} ↑{ob:5.0f}K" if active else "idle"
            put(scr, y, x_num, num, pa)
        if len(socks) > rows:
            put(scr, 5 + rows, x_proc, f"+ {len(socks) - rows} more sockets…",
                cp(C_WHITE) | curses.A_DIM)

    def hud(self, st):
        socks = st.get("sockets", [])
        busy = [s for s in socks if (s["in_bps"] + s["out_bps"]) > 1]
        top = busy[0] if busy else None
        return [("rows = real sockets", f"{len(socks)}"),
                ("flow = that socket's B/s", "‹ in   › out"),
                ("color = remote port", "443 80 53 22 …"),
                ("busiest", f"{top['proc']} {top['remote']}"[:34] if top else "—")]


# ============================================================================
# THEME 5 : NET GRID  (cyberpunk data-tower skyline; flows pinned by a window)
# ============================================================================
class GridScene(Scene):
    """A neon skyline of the busiest sockets. Tower HEIGHT = a connection's
    activity smoothed over a rolling time window (so heavy flows stay pinned
    and stable), while ▲/▼ pulses climb each tower at its live up/down rate."""
    name = "grid"
    title = "▚ N E T   G R I D ▚"
    WINDOW = 30.0     # EMA half-life in seconds — the "memory" of the ranking
    FORGET = 40.0     # drop a faded, absent connection after this long

    def build(self):
        self.track = {}     # key -> {ema,in,out,proc,remote,seen}
        self.slots = {}     # key -> column index (sticky => pinned)
        self.phase = {}     # key -> animation phase
        self.t = 0.0
        self.drip = []      # background datastream rain

    def _key(self, s):
        return s["proto"] + s["local"] + s["remote"]

    def _cols(self):
        return max(1, (self.w - 4) // 13)

    def update(self, dt, frame, st):
        self.t += dt
        alpha = 1 - 0.5 ** (dt / self.WINDOW)
        cur = {self._key(s): s for s in st.get("sockets", [])}
        for k, s in cur.items():
            r = s["in_bps"] + s["out_bps"]
            t = self.track.get(k)
            if not t:
                t = self.track[k] = {"ema": r, "in": 0.0, "out": 0.0,
                                     "proc": s["proc"], "remote": s["remote"]}
            t["ema"] += (r - t["ema"]) * alpha
            t["in"], t["out"] = s["in_bps"], s["out_bps"]
            t["proc"], t["remote"], t["seen"] = s["proc"], s["remote"], self.t
            self.phase[k] = self.phase.get(k, 0.0) + dt * (1.5 + norm_log(r / 1024, 4000) * 22)
        # decay & forget connections that have gone away
        for k, t in list(self.track.items()):
            if k not in cur:
                t["ema"] *= (1 - alpha)
                t["in"] = t["out"] = 0.0
                if t["ema"] < 1 and self.t - t.get("seen", 0) > self.FORGET:
                    self.track.pop(k, None); self.slots.pop(k, None); self.phase.pop(k, None)
        # rank by windowed activity, assign sticky column slots (the "pinning")
        K = self._cols()
        ranked = sorted(self.track, key=lambda k: -self.track[k]["ema"])[:K]
        for k in list(self.slots):
            if k not in ranked:
                self.slots.pop(k, None)
        for k in ranked:
            if k not in self.slots:
                taken = set(self.slots.values())
                for slot in range(K):
                    if slot not in taken:
                        self.slots[k] = slot
                        break
        # datastream rain (kept sparse so the neon towers stay readable)
        f = dt * REF_FPS
        if random.random() < 0.28 * f and len(self.drip) < self.w // 4:
            self.drip.append([0.0, random.randint(2, max(2, self.w - 2)),
                              random.uniform(0.4, 1.1), random.choice("01:|.")])
        for d in self.drip:
            d[0] += d[2] * f
        self.drip = [d for d in self.drip if d[0] < self.h - 4]

    def draw(self, scr, frame, st):
        floor = self.h - 4
        top = 5
        # datastream rain (behind everything)
        for d in self.drip:
            putch(scr, d[0], d[1], d[3], cp(C_GREEN) | curses.A_DIM)
        # neon perspective floor
        for x in range(2, self.w - 2):
            putch(scr, floor, x, "┼" if x % 6 == 0 else "─", cp(C_MAGENTA) | curses.A_DIM)
        for gy in (floor + 3,):
            for x in range(2, self.w - 2, 3):
                putch(scr, gy, x, "·", cp(C_MAGENTA) | curses.A_DIM)
        # header
        center(scr, 2, "NET GRID — sockets ranked over a live window",
               cp(C_CYAN) | curses.A_BOLD)
        tin = sum(t["in"] for t in self.track.values()) / 1024.0
        tout = sum(t["out"] for t in self.track.values()) / 1024.0
        put(scr, 3, 3, f"{self.tele.host}  ::  flows {len(self.track)}  "
                       f"window {int(self.WINDOW)}s  ::  Σ ↓{tin:,.0f} ↑{tout:,.0f} KB/s",
            cp(C_MAGENTA) | curses.A_BOLD)
        if not self.track:
            center(scr, self.h // 2, "… jacking in … (needs nettop) …",
                   cp(C_YELLOW) | curses.A_DIM)
            return
        K = self._cols()
        colw = (self.w - 4) // K
        tw = max(3, colw - 3)
        avail = max(2, floor - top - 1)
        for k, slot in self.slots.items():
            t = self.track[k]
            x0 = 3 + slot * colw
            ema_n = norm_log(t["ema"] / 1024.0, 4000)
            ht = max(1, int(avail * ema_n)) if ema_n > 0.01 else 1
            port = remote_port(t["remote"])
            col = PORT_COLOR.get(port, C_CYAN)
            live = norm_log((t["in"] + t["out"]) / 1024.0, 4000)
            # the tower
            for row in range(ht):
                yy = floor - 1 - row
                frac = row / max(1, ht)
                ch = "█" if frac < 0.45 else ("▓" if frac < 0.75 else "▒")
                attr = cp(col) | (curses.A_BOLD if live > 0.15 else curses.A_DIM)
                for xx in range(tw):
                    putch(scr, yy, x0 + xx, ch, attr)
            ph = self.phase.get(k, 0.0)
            # ▲ upload pulse climbs; ▼ download pulse descends — live rate driven
            if t["out"] > 1:
                yy = floor - 1 - int(ph * 2) % ht
                for xx in range(tw):
                    putch(scr, yy, x0 + xx, "▲", cp(C_YELLOW) | curses.A_BOLD)
            if t["in"] > 1:
                yy = floor - ht + int(ph * 2) % ht
                for xx in range(tw):
                    putch(scr, yy, x0 + xx, "▼", cp(C_GREEN) | curses.A_BOLD)
            # labels: remote at the spire, process at the base, live rate below
            put(scr, floor - ht - 1, x0, t["remote"][:tw + 2], cp(col) | curses.A_BOLD)
            put(scr, floor + 1, x0, t["proc"][:tw + 2], cp(C_WHITE))
            kbs = (t["in"] + t["out"]) / 1024.0
            put(scr, floor + 2, x0, (f"{kbs:,.0f}K/s" if kbs >= 1 else "idle"),
                cp(col) | curses.A_DIM)

    def hud(self, st):
        ranked = sorted(self.track.values(), key=lambda t: -t["ema"])
        top = ranked[0] if ranked else None
        return [("towers = sockets", f"{len(self.slots)} pinned / {len(self.track)}"),
                ("height = windowed B/s", f"{int(self.WINDOW)}s memory"),
                ("▲ up  ▼ down  pulses", "live rate"),
                ("top flow", (f"{top['proc']} {top['remote']}"[:32]) if top else "—")]


# ============================================================================
# THEME 6 : HARBOR  (Docker containers as ships in an animated harbor)
# ============================================================================
HULL_W = 11
SHIP_COL = HULL_W + 5
WHALE = ["  [#][#][#]", "<(___________)", "  \\~~~~~~~~~/"]
CARGO_COLORS = [C_CYAN, C_YELLOW, C_MAGENTA, C_GREEN]


class HarborScene(Scene):
    """Each Docker container is a ship: cargo stack = memory, funnel smoke =
    CPU, wake = network I/O, crew count in the label = PIDs, hull color = state.
    Ships rise in when a container starts and sink when one stops; the Docker
    whale cruises through now and then."""
    name = "harbor"
    title = "⚓ H A R B O R ⚓"

    def build(self):
        self.t = 0.0
        self.ship = {}          # name -> {smoke, born, last}
        self.slots = {}         # name -> column index
        self.sinking = {}       # name -> {t0, data, slot}
        self.known = set()
        self.whale = None
        self.whale_t = 8.0

    def _cols(self):
        return max(1, (self.w - 2) // SHIP_COL)

    def update(self, dt, frame, st):
        self.t += dt
        conts = st.get("docker", [])
        running = [c for c in conts if c.get("state") == "running"]
        running.sort(key=lambda c: -(c["cpu"] + c["mem"]))
        K = self._cols()
        shown = running[:K]
        names = {c["name"] for c in shown}
        # detect departures -> start sinking animation
        for n in list(self.known):
            if n not in names and n in self.slots:
                data = self.ship.get(n, {}).get("last")
                if data:
                    self.sinking[n] = {"t0": self.t, "data": data,
                                       "slot": self.slots[n]}
                self.slots.pop(n, None)
                self.ship.pop(n, None)
        # assign sticky slots to current ships
        for n in list(self.slots):
            if n not in names:
                self.slots.pop(n, None)
        for c in shown:
            n = c["name"]
            if n not in self.slots:
                taken = set(self.slots.values()) | {s["slot"] for s in self.sinking.values()}
                for sl in range(K):
                    if sl not in taken:
                        self.slots[n] = sl
                        break
            sh = self.ship.setdefault(n, {"smoke": [], "born": self.t})
            sh["last"] = c
            # smoke puffs from the funnel, rate scales with CPU
            if random.random() < (0.06 + c["cpu"] * 0.9) * dt * REF_FPS:
                sh["smoke"].append([0.0, random.uniform(-0.6, 0.6),
                                    random.choice("░▒o°")])
            for p in sh["smoke"]:
                p[0] += dt * (2.2 + c["cpu"] * 3)
                p[1] += dt * 0.6
            sh["smoke"] = [p for p in sh["smoke"] if p[0] < 7]
        self.known = names
        # expire sinking ships
        for n in list(self.sinking):
            if self.t - self.sinking[n]["t0"] > 2.0:
                self.sinking.pop(n, None)
        # whale scheduling
        self.whale_t -= dt
        if self.whale is None and self.whale_t <= 0:
            d = random.choice([-1, 1])
            self.whale = {"x": float(-14 if d > 0 else self.w + 2), "dir": d,
                          "y": 4 + random.randint(0, max(0, self.h // 3))}
        if self.whale:
            self.whale["x"] += dt * 9 * self.whale["dir"]
            if (self.whale["dir"] > 0 and self.whale["x"] > self.w + 2) or \
               (self.whale["dir"] < 0 and self.whale["x"] < -16):
                self.whale = None
                self.whale_t = random.uniform(25, 55)

    def _state_color(self, state):
        if state == "running":
            return C_GREEN
        if state in ("restarting", "dead"):
            return C_RED
        if state in ("created", "paused"):
            return C_YELLOW
        return C_WHITE      # exited

    def _draw_ship(self, scr, cx, wy, c, smoke, rise):
        hw = HULL_W
        half = hw // 2
        x0 = cx - half
        col = self._state_color(c.get("state", "running"))
        dim = curses.A_DIM if c.get("state") != "running" else 0
        wy = int(wy + rise)
        # hull
        put(scr, wy, x0, "\\" + "_" * (hw - 2) + "/", cp(col) | curses.A_BOLD | dim)
        put(scr, wy - 1, x0, "|" + c["name"][:hw - 2].center(hw - 2) + "|",
            cp(col) | dim)
        put(scr, wy - 2, x0, "/" + "≡" * (hw - 2) + "\\", cp(col) | curses.A_BOLD | dim)
        # cargo stack — height scales with memory
        stack = 1 + int(c["mem"] * 4) if c.get("state") == "running" else 1
        for r in range(stack):
            y = wy - 3 - r
            cc = CARGO_COLORS[r % len(CARGO_COLORS)]
            put(scr, y, x0 + 1, "[" + "■" * (hw - 4) + "]", cp(cc) | curses.A_BOLD | dim)
        # funnel + smoke (only when running)
        if c.get("state") == "running":
            fy = wy - 3 - stack
            putch(scr, fy, cx, "╫", cp(C_WHITE) | curses.A_BOLD)
            scol = C_RED if c["cpu"] > 0.5 else (C_YELLOW if c["cpu"] > 0.2 else C_WHITE)
            for (py, px, ch) in smoke:
                putch(scr, fy - 1 - py, cx + int(px), ch,
                      cp(scol) | (curses.A_BOLD if c["cpu"] > 0.3 else curses.A_DIM))
        # wake to the left, length scales with network I/O
        wlen = int(norm_log(c.get("net_bps", 0) / 1024.0, 3000) * 8)
        for i in range(wlen):
            x = x0 - 2 - i
            ch = "≈" if (i + int(self.t * 8)) % 2 == 0 else "~"
            putch(scr, wy, x, ch, cp(C_CYAN) | curses.A_DIM)
        # labels under the water
        put(scr, wy + 2, x0 - 1, c["name"][:hw + 2], cp(col) | curses.A_BOLD | dim)
        put(scr, wy + 3, x0 - 1, c.get("image", "")[:hw + 2], cp(C_WHITE) | curses.A_DIM)
        if c.get("state") == "running":
            put(scr, wy + 4, x0 - 1,
                f"{c['cpu']*100:.0f}% ▭{c['mem']*100:.0f}% ⋯{c['pids']}",
                cp(col) | curses.A_DIM)
        else:
            put(scr, wy + 4, x0 - 1, c.get("state", "exited"), cp(C_WHITE) | curses.A_DIM)

    def draw(self, scr, frame, st):
        wy = self.h - 6
        # sky decor
        center(scr, 2, "HARBOR — your Docker fleet", cp(C_CYAN) | curses.A_BOLD)
        if not st.get("docker_ok"):
            center(scr, self.h // 2, "⚓  no docker daemon reachable  ⚓",
                   cp(C_YELLOW) | curses.A_DIM)
        n_run = sum(1 for c in st.get("docker", []) if c.get("state") == "running")
        put(scr, 3, 3, f"{self.tele.host}  ::  {n_run} afloat / {len(st.get('docker', []))} total",
            cp(C_CYAN) | curses.A_DIM)
        # whale (behind ships)
        if self.whale:
            for i, ln in enumerate(WHALE):
                put(scr, self.whale["y"] + i, int(self.whale["x"]), ln,
                    cp(C_BLUE) | curses.A_BOLD)
        # ships
        K = self._cols()
        running = sorted([c for c in st.get("docker", []) if c.get("state") == "running"],
                         key=lambda c: -(c["cpu"] + c["mem"]))[:K]
        for c in running:
            n = c["name"]
            slot = self.slots.get(n)
            if slot is None:
                continue
            cx = 2 + slot * SHIP_COL + SHIP_COL // 2
            sh = self.ship.get(n, {})
            born = sh.get("born", self.t)
            rise = max(0.0, 1.0 - (self.t - born) / 1.2) * 5   # rise-in on arrival
            self._draw_ship(scr, cx, wy, c, sh.get("smoke", []), rise)
        # sinking ships
        for n, sk in list(self.sinking.items()):
            prog = (self.t - sk["t0"]) / 2.0
            cx = 2 + sk["slot"] * SHIP_COL + SHIP_COL // 2
            self._draw_ship(scr, cx, wy, sk["data"], [], prog * 6)
            putch(scr, wy, cx, random.choice("∴∵·"), cp(C_WHITE) | curses.A_DIM)
        # waterline (drawn last, over hull bottoms for a "floating" look)
        for x in range(2, self.w - 2):
            crest = (x + int(self.t * 6)) % 7 < 2
            putch(scr, wy + 1, x, "≈" if crest else "~",
                  cp(C_BLUE) | (curses.A_BOLD if crest else curses.A_DIM))

    def hud(self, st):
        conts = st.get("docker", [])
        run = [c for c in conts if c.get("state") == "running"]
        top = max(run, key=lambda c: c["cpu"], default=None)
        return [("ships = containers", f"{len(run)} up / {len(conts)}"),
                ("cargo = memory", "stack height"),
                ("smoke = cpu", "hotter = redder"),
                ("wake = net I/O", "longer = busier"),
                ("busiest", (top["name"]) if top else "—")]


# ============================================================================
# THEME 7 : GITHUB  (the contribution heatmap + account stats)
# ============================================================================
# intensity level -> (glyph, color, attr)
GH_LEVELS = [("·", C_WHITE, curses.A_DIM), ("░", C_GREEN, 0),
             ("▒", C_GREEN, 0), ("▓", C_GREEN, curses.A_BOLD),
             ("█", C_GREEN, curses.A_BOLD)]
GH_DOW = {1: "Mon", 3: "Wed", 5: "Fri"}


class GithubScene(Scene):
    """Renders the live GitHub contribution calendar as an animated heatmap,
    with a panel of account stats (followers, repos, stars, PRs, notifications,
    streaks). A shimmer sweeps across the grid; today's cell pulses."""
    name = "github"
    title = "▦ G I T H U B ▦"

    def build(self):
        self.t = 0.0

    def update(self, dt, frame, st):
        self.t += dt

    def draw(self, scr, frame, st):
        gh = st.get("github", {})
        center(scr, 2, "GITHUB — contribution graph", cp(C_GREEN) | curses.A_BOLD)
        if not gh.get("ok"):
            center(scr, self.h // 2,
                   "fetching from GitHub…" if self.tele.gh_ok else "gh not authenticated",
                   cp(C_YELLOW) | curses.A_DIM)
            return
        # stats panel
        put(scr, 3, 3, f"@{gh['login']}", cp(C_GREEN) | curses.A_BOLD)
        priv = (f"  (incl. +{gh['private']} from {gh['tracked']} private repo"
                f"{'s' if gh.get('tracked', 0) != 1 else ''})") if gh.get("private") else ""
        put(scr, 3, 6 + len(gh['login']),
            f" ·  {gh['total']} contributions ·  streak {gh['cur']}d ·  longest {gh['longest']}d{priv}",
            cp(C_WHITE))
        put(scr, 4, 3,
            f"followers {gh['followers']}   following {gh['following']}   "
            f"repos {gh['repos']}   ★ {gh['stars']}   open PRs {gh['prs']}   "
            f"⚑ {gh['notif']}   API {gh['rate']}/5000",
            cp(C_WHITE) | curses.A_DIM)

        grid = gh.get("grid", [])
        if not grid:
            return
        top, left = 7, 6
        avail = max(8, self.w - left - 2)
        shown = min(len(grid), avail)
        start = len(grid) - shown
        # weekday labels
        for wd, lab in GH_DOW.items():
            put(scr, top + wd, 1, lab, cp(C_WHITE) | curses.A_DIM)
        # shimmer sweep position (in shown-column space)
        sweep = int(self.t * 12) % max(1, shown)
        last_wi = shown - 1
        for wi in range(shown):
            week = grid[start + wi]
            x = left + wi
            for wd in range(7):
                lv = week[wd]
                if lv < 0:
                    continue
                glyph, color, attr = GH_LEVELS[lv]
                a = cp(color) | attr
                if wi == sweep:                      # shimmer brightening
                    a |= curses.A_REVERSE
                # pulse the most recent populated cell (≈ today)
                if wi == last_wi and lv >= 0 and int(self.t * 3) % 2 == 0:
                    a = cp(C_GREEN) | curses.A_BOLD | curses.A_REVERSE
                putch(scr, top + wd, x, glyph, a)
        # legend
        ly = top + 8
        put(scr, ly, left, "less ", cp(C_WHITE) | curses.A_DIM)
        for i, (g, c, at) in enumerate(GH_LEVELS):
            putch(scr, ly, left + 5 + i, g, cp(c) | at)
        put(scr, ly, left + 5 + len(GH_LEVELS) + 1, "more", cp(C_WHITE) | curses.A_DIM)

    def hud(self, st):
        gh = st.get("github", {})
        if not gh.get("ok"):
            return [("status", "fetching / not authed")]
        return [("grid = contributions", "past year"),
                ("streak", f"{gh.get('cur', 0)}d (max {gh.get('longest', 0)})"),
                ("stars", str(gh.get("stars", 0))),
                ("open PRs", str(gh.get("prs", 0))),
                ("notifications", str(gh.get("notif", 0)))]


# ============================================================================
# THEME 8 : OCTO-PET  (a Tamagotchi whose mood reflects your GitHub stats)
# ============================================================================
class OctoPetScene(Scene):
    """A little ASCII companion that lives off your GitHub activity: a long
    contribution streak keeps it energetic and happy, idleness makes it sleepy/
    hungry; it's orbited by your ★ stars, ♥ followers, and ! notifications,
    carries your open PRs, and chomps commit pellets when you've been active."""
    name = "octopet"
    title = "◓ O C T O - P E T ◓"

    def build(self):
        self.t = 0.0
        self.blink = 0.0
        self.pellets = []
        self.chomp = 0.0
        self.msg_i = 0
        self.msg_t = 0.0

    def _vitals(self, gh):
        streak = gh.get("cur", 0)
        grid = gh.get("grid", [])
        recent = [lv for wk in grid for lv in wk if lv >= 0][-5:]
        fed = (sum(recent) / (4 * len(recent))) if recent else 0.0
        energy = clamp(streak / 14.0)
        today = recent[-1] if recent else 0
        return streak, energy, fed, today

    def _mood(self, gh, streak, energy, fed, today):
        # (label, eyes, mouth, color)
        if not gh.get("ok"):
            return ("WAKING", "- -", "...", C_WHITE)
        if streak >= 7:
            return ("ECSTATIC", "★ ★", "▽", C_YELLOW)
        if streak >= 3:
            return ("HAPPY", "^ ^", "‿", C_GREEN)
        if today > 0 or fed > 0.4:
            return ("CONTENT", "o o", "·", C_CYAN)
        if streak == 0 and fed < 0.15:
            return ("SLEEPY", "- -", "~", C_BLUE)
        return ("HUNGRY", "o o", "ᵕ", C_MAGENTA)

    def _speak(self, gh, mood, streak):
        opts = {
            "ECSTATIC": [f"{streak}-day streak!! unstoppable!", "shipping like a legend"],
            "HAPPY": [f"{streak}-day streak — keep it up!", "feeling productive"],
            "CONTENT": ["fed and happy.", "good commits today"],
            "SLEEPY": ["zzz… push something?", "so quiet here…"],
            "HUNGRY": ["feed me a commit…", "i could go for a PR"],
            "WAKING": ["…", "booting up"],
        }[mood]
        if gh.get("ok"):
            if gh.get("notif"):
                opts.append(f"{gh['notif']} notifications!")
            if gh.get("prs"):
                opts.append(f"{gh['prs']} PRs waiting on you")
            if gh.get("stars"):
                opts.append(f"{gh['stars']} stars — nice!")
        return opts[self.msg_i % len(opts)]

    def update(self, dt, frame, st):
        self.t += dt
        self.blink += dt
        self.msg_t += dt
        if self.msg_t > 4.0:
            self.msg_t = 0.0
            self.msg_i += 1
        gh = st.get("github", {})
        _, _, fed, today = self._vitals(gh) if gh else (0, 0, 0, 0)
        # commit pellets drift in when there's recent activity; pet chomps them
        cx, cy = self.w // 2, self.h // 2 + 1
        if random.random() < (0.02 + today * 0.06) * dt * REF_FPS:
            self.pellets.append([float(random.choice([3, self.w - 4])),
                                 float(random.randint(3, max(3, self.h - 4)))])
        for p in self.pellets:
            p[0] += (cx - p[0]) * min(1.0, dt * 1.5)
            p[1] += (cy - p[1]) * min(1.0, dt * 1.5)
        eaten = [p for p in self.pellets if abs(p[0] - cx) < 2 and abs(p[1] - cy) < 2]
        if eaten:
            self.chomp = 0.4
        self.pellets = [p for p in self.pellets if p not in eaten]
        self.chomp = max(0.0, self.chomp - dt)

    def _orbit(self, scr, cx, cy, glyph, color, count, radius, speed):
        import math
        for i in range(count):
            a = self.t * speed + i * 2 * math.pi / max(1, count)
            yy = cy + radius * 0.5 * math.sin(a)
            xx = cx + radius * math.cos(a)
            putch(scr, yy, xx, glyph, cp(color) | curses.A_BOLD)

    def draw(self, scr, frame, st):
        gh = st.get("github", {})
        cx, cy = self.w // 2, self.h // 2 + 1
        center(scr, 2, "OCTO-PET — your GitHub familiar", cp(C_GREEN) | curses.A_BOLD)
        if not gh.get("ok"):
            center(scr, 3, "fetching from GitHub…" if self.tele.gh_ok
                   else "gh not authenticated", cp(C_YELLOW) | curses.A_DIM)

        streak, energy, fed, today = self._vitals(gh)
        mood, eyes, mouth, mcol = self._mood(gh, streak, energy, fed, today)
        # blink
        if (self.blink % 4.0) < 0.15:
            eyes = "- -"
        if self.chomp > 0:
            mouth = "▽"

        # orbiters: stars / followers / notifications
        if gh.get("ok"):
            self._orbit(scr, cx, cy, "★", C_YELLOW, min(gh.get("stars", 0), 8), 12, 0.6)
            self._orbit(scr, cx, cy, "♥", C_MAGENTA, min(gh.get("following", 0)
                        + gh.get("followers", 0), 6), 9, -0.45)
            nb = gh.get("notif", 0)
            nb = nb if isinstance(nb, int) else 0       # may be "—" for --gh-user
            self._orbit(scr, cx, cy, "!", C_RED,
                        min((nb // 8) + (1 if nb else 0), 8), 8, 1.3)

        # commit pellets
        for p in self.pellets:
            putch(scr, p[1], p[0], "◦", cp(C_GREEN) | curses.A_BOLD)

        # the pet (bobbing)
        import math
        bob = int(round(math.sin(self.t * 2) * 0.5))
        y = cy + bob
        ph = int(self.t * 6)
        tent = "".join("v" if (i + ph) % 2 else "V" for i in range(5))
        put(scr, y - 2, cx - 3, " /\\_/\\ ", cp(mcol) | curses.A_BOLD)
        put(scr, y - 1, cx - 3, f"( {eyes} )", cp(mcol) | curses.A_BOLD)
        put(scr, y,     cx - 3, f" >{mouth.center(3)}< ", cp(mcol) | curses.A_BOLD)
        put(scr, y + 1, cx - 3, f" /{tent}\\ ", cp(C_MAGENTA))
        put(scr, y + 2, cx - 6, "‗" * 13, cp(C_WHITE) | curses.A_DIM)  # ground

        # speech bubble
        if gh.get("ok"):
            msg = self._speak(gh, mood, streak)
            bx = cx - len(msg) // 2 - 2
            put(scr, y - 4, bx, f"( {msg} )", cp(C_WHITE) | curses.A_BOLD)
            putch(scr, y - 3, cx - 2, "o", cp(C_WHITE))

        # PRs carried as packages on the ground beside the pet
        prs = gh.get("prs", 0) if gh.get("ok") else 0
        if isinstance(prs, int):
            for i in range(min(prs, 6)):
                put(scr, y + 1, cx + 6 + i * 2, "▣", cp(C_YELLOW) | curses.A_BOLD)
            if prs:
                put(scr, y + 2, cx + 6, f"PRs:{prs}", cp(C_YELLOW) | curses.A_DIM)

        # vitals panel
        if gh.get("ok"):
            py = self.h - 5
            self._bar(scr, py, 3, "ENERGY", energy, C_GREEN)
            self._bar(scr, py + 1, 3, "FED", fed, C_YELLOW)
            put(scr, py + 2, 3, f"MOOD: {mood}", cp(mcol) | curses.A_BOLD)
            put(scr, py, self.w - 40, f"@{gh['login']}", cp(C_GREEN) | curses.A_BOLD)
            put(scr, py + 1, self.w - 40,
                f"streak {streak}d  best {gh.get('longest',0)}d  "
                f"{gh.get('total',0)} contribs", cp(C_WHITE) | curses.A_DIM)
            put(scr, py + 2, self.w - 40,
                f"★{gh.get('stars',0)} ◍{gh.get('followers',0)} "
                f"⇄{gh.get('prs',0)} ⚑{gh.get('notif',0)}", cp(C_WHITE) | curses.A_DIM)

    def _bar(self, scr, y, x, label, frac, color, wd=10):
        n = int(clamp(frac) * wd)
        put(scr, y, x, f"{label:7}[", cp(C_WHITE))
        put(scr, y, x + 8, "█" * n + "░" * (wd - n), cp(color) | curses.A_BOLD)
        put(scr, y, x + 8 + wd, "]", cp(C_WHITE))

    def hud(self, st):
        gh = st.get("github", {})
        if not gh.get("ok"):
            return [("status", "fetching / not authed")]
        _, energy, fed, _ = self._vitals(gh)
        return [("mood = streak/activity", f"{gh.get('cur',0)}d streak"),
                ("orbiters", "★stars ♥followers !notif"),
                ("packages = open PRs", str(gh.get("prs", 0))),
                ("pellets = recent commits", "")]


THEMES = [CosmosScene, BoilerScene, SignalScene, SocketsScene, GridScene,
          HarborScene, GithubScene, OctoPetScene]
THEME_BY_NAME = {c.name: c for c in THEMES}

# Register the built-in boards. Gating that used to be hardcoded in run_app is
# expressed as each board's available() below, so the orchestrator just asks.
for _c in THEMES:
    BOARDS.setdefault(_c.name, _c)
HarborScene.available = classmethod(lambda cls, tele, cfg=None: bool(tele.docker_ok))
GithubScene.available = classmethod(lambda cls, tele, cfg=None: bool(tele.gh_ok))
OctoPetScene.available = classmethod(lambda cls, tele, cfg=None: bool(tele.gh_ok))


# ----------------------------------------------------------------------------
# Plugin boards + manifest
# ----------------------------------------------------------------------------
# Let plugin board files (driftboards/*.py and ~/.drift/driftboards/*.py) import
# the core via `from driftcore import ...` without re-executing this module.
sys.modules.setdefault("driftcore", sys.modules[__name__])


def load_plugin_boards():
    """Import every driftboards/*.py so its @board registrations run. Looks
    next to drift.py and in ~/.drift/driftboards. Import errors are reported but
    never fatal — a broken plugin shouldn't take drift down."""
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = [os.path.join(here, "driftboards"),
            os.path.expanduser("~/.drift/driftboards")]
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(d, fn)
            modname = "driftboard_" + os.path.splitext(fn)[0]
            try:
                spec = importlib.util.spec_from_file_location(modname, path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[modname] = mod
                spec.loader.exec_module(mod)
            except Exception as e:                       # never fatal
                sys.stderr.write(f"drift: skipped plugin {fn}: {e}\n")


def validate_config(cls, cfg):
    """Apply a board's CONFIG schema to a manifest config dict: fill defaults
    and cast declared types. Returns a new dict; unknown keys pass through."""
    out = dict(cfg or {})
    for key, spec in (cls.CONFIG or {}).items():
        if key not in out:
            out[key] = spec.get("default")
        cast = spec.get("cast")
        if cast and out[key] is not None:
            try:
                out[key] = cast(out[key])
            except (TypeError, ValueError):
                out[key] = spec.get("default")
    return out


def load_manifest(path):
    """Read a driftboards manifest (JSON). Returns (entries, rotation) where
    entries is a list of {type, label, config} and rotation is a dict of
    overrides. Returns (None, {}) if there's no manifest (-> default behavior).
    A malformed manifest is reported and treated as absent."""
    if not path or not os.path.isfile(path):
        return None, {}
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write(f"drift: ignoring manifest ({e})\n")
        return None, {}
    entries = []
    for raw in doc.get("boards", []):
        t = raw.get("type")
        if t:
            entries.append({"type": t, "label": raw.get("label"),
                            "config": raw.get("config", {})})
    return entries, doc.get("rotation", {})


# ----------------------------------------------------------------------------
# Border, HUD overlay, transitions
# ----------------------------------------------------------------------------
def draw_border(scr, h, w, title):
    a = cp(C_BLUE) | curses.A_DIM
    for x in range(w):
        putch(scr, 0, x, "─", a)
        putch(scr, h - 1, x, "─", a)
    for y in range(h):
        putch(scr, y, 0, "│", a)
        putch(scr, y, w - 1, "│", a)
    put(scr, 0, 3, f" {title} ", cp(C_WHITE) | curses.A_BOLD)
    tag = " drift  ·  q quit  space skip  n/p theme  l lock  h hud  f fps  +/- speed "
    put(scr, h - 1, max(3, w - len(tag) - 3), tag, a)


def draw_hud(scr, h, w, scene, st):
    lines = [("clock", time.strftime("%H:%M:%S")),
             ("uptime", f"{int(st['uptime']//86400)}d {int(st['uptime']%86400//3600)}h"),
             ("load", " ".join(f"{x:.2f}" for x in st["load"])),
             ("mem", f"{st['mem']*100:.0f}%"),
             ("disk", f"{st['disk']*100:.0f}%"),
             ("battery", (f"{st['batt']}%" + (" chg" if st['charging'] else "")) if st['batt'] is not None else "—"),
             ("procs", str(st["procs"]))]
    lines += scene.hud(st)
    bw = max(len(f"{a}: {b}") for a, b in lines) + 2
    y0 = 2
    x0 = w - bw - 2
    put(scr, y0 - 1, x0, "┌ DECODER " + "─" * max(0, bw - 9), cp(C_WHITE))
    for i, (a, b) in enumerate(lines):
        put(scr, y0 + i, x0, f"{a}: {b}"[:bw], cp(C_WHITE) | curses.A_DIM)
    put(scr, y0 + len(lines), x0, "└" + "─" * (bw - 1), cp(C_WHITE))


def transition(scr, h, w, label, frame_ref):
    """Quick static-dissolve reveal; returns when done. Honors quit."""
    dur = 0.5
    t0 = time.time()
    while True:
        prog = (time.time() - t0) / dur
        if prog >= 1.0:
            return True
        scr.erase()
        density = 1.0 - prog
        for _ in range(int(h * w * 0.5 * density)):
            putch(scr, random.randint(0, h - 1), random.randint(0, w - 1),
                  random.choice("01:· "), cp(C_GREEN) | curses.A_DIM)
        center(scr, h // 2, f"»  {label}  «", cp(C_CYAN) | curses.A_BOLD)
        scr.refresh()
        k = scr.getch()
        if k in (ord("q"), ord("Q")):
            return False
        time.sleep(0.03)


# ----------------------------------------------------------------------------
# Board resolution (manifest -> rotation of board instances)
# ----------------------------------------------------------------------------
def resolve_boards(opts, tele):
    """Build the rotation as a list of board specs {cls, cfg, label}.

    With a manifest: each entry's type is looked up in the registry, its config
    validated/defaulted, and it's kept only if available(). Without a manifest
    (or if it yields nothing usable): every available built-in board, no config
    — i.e. drift's original behavior is preserved. Returns (specs, rotation)."""
    entries, rotation = load_manifest(getattr(opts, "manifest", None))
    specs = []
    if entries:
        for e in entries:
            cls = BOARDS.get(e["type"])
            if not cls:
                sys.stderr.write(f"drift: unknown board '{e['type']}'\n")
                continue
            cfg = validate_config(cls, e.get("config"))
            if not cls.available(tele, cfg):
                continue
            specs.append({"cls": cls, "cfg": cfg,
                          "label": e.get("label") or cls.name})
    if not specs:
        for cls in THEMES:
            if cls.available(tele, None):
                specs.append({"cls": cls, "cfg": {}, "label": cls.name})
    return specs, rotation


def make_board(spec, h, w, tele):
    return spec["cls"](h, w, tele, spec["cfg"])


def start_fetchers(specs, tele):
    """For every spec whose board declares an `interval`, run its fetch(cfg) on
    a background daemon thread and stash the latest result in spec['data'] (an
    atomic dict swap — the render loop reads it lock-free). Fetchers run for the
    whole session so a board's data is warm before it rotates into view; they
    stop when tele.running goes False. Exceptions become an {'error': ...} dict
    rendered as an error card, never a crash."""
    def loop(spec):
        cls, cfg = spec["cls"], spec["cfg"]
        inst = cls.__new__(cls)            # fetch needs no render state
        inst.tele, inst.cfg = tele, cfg
        while tele.running:
            try:
                spec["data"] = inst.fetch(cfg) or {}
            except Exception as e:         # noqa: BLE001 — never take drift down
                spec["data"] = {"error": f"{type(e).__name__}: {e}"}
            slept = 0.0
            while tele.running and slept < cls.interval:
                time.sleep(0.5); slept += 0.5
    for spec in specs:
        if spec["cls"].interval:
            spec.setdefault("data", {})
            threading.Thread(target=loop, args=(spec,), daemon=True).start()


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def run_app(scr, opts, tele, monitor=None):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    init_colors()
    scr.nodelay(True)
    fps = float(opts.fps)
    show_hud = opts.hud
    show_fps = not opts.no_fps_meter
    frames = 0            # cumulative frames rendered
    fps_meas = fps        # smoothed, actually-achieved frame rate
    kps = 0.0             # smoothed keystrokes/sec (keys typed INTO drift only)
    ktotal = 0            # cumulative keystrokes
    kacc = 0              # keystrokes seen this frame
    sparks = []           # typing-energy particles: [x,y,vy,vx,char,color]
    speed = 1.0

    h, w = scr.getmaxyx()
    # rotation pool of board specs (from manifest, or all available built-ins)
    specs, rotation = resolve_boards(opts, tele)
    start_fetchers(specs, tele)          # background data pulls for fetch boards
    min_secs = rotation.get("min_secs", opts.min_secs)
    max_secs = rotation.get("max_secs", opts.max_secs)
    if opts.theme and opts.theme in BOARDS:
        # lock to the requested board; ensure it's present so n/p can land on it
        idx = next((i for i, s in enumerate(specs)
                    if s["cls"].name == opts.theme), None)
        if idx is None:
            specs.append({"cls": BOARDS[opts.theme], "cfg": {},
                          "label": opts.theme})
            idx = len(specs) - 1
        cur = idx
        locked = True
    else:
        cur = random.randrange(len(specs))
        locked = rotation.get("lock", opts.lock)
    scene = make_board(specs[cur], h, w, tele)
    seg = random.uniform(min_secs, max_secs)
    seg_start = time.time()
    last = time.time()
    aframe = 0.0          # animation clock in REF_FPS "frames" (fps-independent)
    force_switch = False

    end_at = (time.time() + opts.minutes * 60) if opts.minutes else None

    while True:
        now = time.time()
        raw_dt = now - last                       # real frame interval
        dt = min(0.2, raw_dt) * speed
        last = now
        frames += 1
        if raw_dt > 0:                            # smoothed measured fps (EMA)
            fps_meas += (1.0 / raw_dt - fps_meas) * 0.1
        # advance the animation clock at the reference rate, not the real fps,
        # so frame%/frame// cyclic animations don't speed up at higher fps
        aframe += dt * REF_FPS
        frame = int(aframe)

        # resize
        nh, nw = scr.getmaxyx()
        if (nh, nw) != (h, w):
            h, w = nh, nw
            scene.resize(h, w)

        # input
        k = scr.getch()
        while k != -1:
            if k != curses.KEY_RESIZE:
                kacc += 1; ktotal += 1     # count every key typed into drift
            if k in (ord("q"), ord("Q")):
                return
            elif k == ord(" "):
                force_switch = True         # skip now, even if locked
            elif k in (ord("n"), ord("p")):
                cur = (cur + (1 if k == ord("n") else -1)) % len(specs)
                scene = make_board(specs[cur], h, w, tele)
                seg_start = now
            elif k in (ord("l"), ord("L")):
                locked = not locked
            elif k in (ord("h"), ord("H")):
                show_hud = not show_hud
            elif k in (ord("f"), ord("F")):
                show_fps = not show_fps
            elif k == ord("+"):
                speed = min(4.0, speed + 0.25)
            elif k == ord("-"):
                speed = max(0.25, speed - 0.25)
            k = scr.getch()

        # typing-rate tracking — in-window keys, plus global key TIMING when the
        # opt-in monitor is active (content-free; see GlobalKeyMonitor)
        gkeys = monitor.pop() if monitor else 0
        keys_this, kacc = kacc + gkeys, 0
        inst = keys_this / max(raw_dt, 1e-3)
        kps += (inst - kps) * (1 - 0.5 ** (raw_dt / 0.4))   # ~0.4s smoothing
        intensity = clamp(kps / 10.0)                        # ~10 keys/s = full
        # energy sparks: a burst per keystroke, rising; rate scales with speed
        for _ in range(min(keys_this * 4, 30)):
            sparks.append([float(random.randint(2, max(2, w - 2))), float(h - 2),
                           -random.uniform(0.4, 1.2), random.uniform(-0.25, 0.25),
                           random.choice("·•*✦°"),
                           random.choice([C_CYAN, C_YELLOW, C_GREEN])])
        fscale = dt * REF_FPS
        for s in sparks:
            s[1] += s[2] * fscale
            s[0] += s[3] * fscale
        sparks = [s for s in sparks if s[1] > 1 and 0 <= s[0] < w]

        st = tele.snapshot()
        st["typing"] = intensity
        st["kps"] = kps
        bd = specs[cur].get("data")          # active board's fetched data (if any)
        if bd:
            st.update(bd)

        # board rotation
        if force_switch or (not locked and (now - seg_start) >= seg):
            force_switch = False
            choices = [i for i in range(len(specs)) if i != cur] or [cur]
            cur = random.choice(choices)
            nxt = specs[cur]
            if not transition(scr, h, w, nxt["cls"].title, frame):
                return
            scene = make_board(nxt, h, w, tele)
            seg = random.uniform(min_secs, max_secs)
            seg_start = time.time()
            last = time.time()
            continue

        scene.update(dt, frame, st)

        scr.erase()
        scene.draw(scr, frame, st)
        # typing-energy sparks (global overlay, intensifies as you type faster)
        for s in sparks:
            bright = curses.A_BOLD if s[1] > h * 0.5 else curses.A_DIM
            putch(scr, s[1], s[0], s[4], cp(s[5]) | bright)
        draw_border(scr, h, w, scene.title)
        if show_hud:
            draw_hud(scr, h, w, scene, st)
        if show_fps:
            wpm = int(kps * 12)                          # ~5 chars/word
            kico = "⌨⚡" if (monitor and monitor.ok) else "⌨"
            meter = f" {fps_meas:4.1f} fps · frame {frames} · {kico} {kps:4.1f}/s {wpm} wpm "
            col = C_GREEN if fps_meas >= fps * 0.9 else (
                C_YELLOW if fps_meas >= fps * 0.6 else C_RED)
            put(scr, h - 1, 3, meter, cp(col) | curses.A_BOLD)
        if monitor and monitor.error:                    # permission/setup hint
            put(scr, 1, 3, f"global keys: {monitor.error}", cp(C_RED) | curses.A_BOLD)
        scr.refresh()

        if end_at and now >= end_at:
            return

        time.sleep(max(0, 1.0 / fps - (time.time() - now)))


def build_parser():
    p = argparse.ArgumentParser(description="drift — an ambient themed terminal.")
    p.add_argument("--theme", metavar="NAME",
                   help="lock to one board by name (built-in or plugin)")
    p.add_argument("--manifest", metavar="PATH",
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "driftboards.json"),
                   help="board manifest (JSON); default ./driftboards.json if present")
    p.add_argument("--minutes", type=float, default=0, help="exit after N minutes")
    p.add_argument("--min-secs", type=float, default=45, dest="min_secs",
                   help="min seconds per scene")
    p.add_argument("--max-secs", type=float, default=110, dest="max_secs",
                   help="max seconds per scene")
    p.add_argument("--fps", type=float, default=60.0,
                   help="target frame rate (default 60; lower to save CPU)")
    p.add_argument("--no-fps-meter", action="store_true", dest="no_fps_meter",
                   help="hide the fps / frame counter (toggle in-app with f)")
    p.add_argument("--gh-user", dest="gh_user", metavar="LOGIN",
                   help="show GitHub stats for this account (public data) "
                        "instead of the active gh account")
    p.add_argument("--gh-repo", dest="gh_repos", metavar="OWNER/REPO",
                   action="append",
                   help="merge YOUR commits from this repo into the contribution "
                        "graph (repeatable; great for private org repos that "
                        "aren't attributed to your account)")
    p.add_argument("--global-keys", action="store_true", dest="global_keys",
                   help="drive the typing meter from ALL typing system-wide "
                        "(needs pyobjc Quartz + macOS Input Monitoring permission; "
                        "captures key TIMING only, never content)")
    p.add_argument("--hud", action="store_true", help="show telemetry decoder HUD")
    p.add_argument("--lock", action="store_true", help="don't auto-rotate themes")
    p.add_argument("--public-ip", action="store_true", dest="public_ip",
                   help="look up public IP (makes ONE outbound request)")
    p.add_argument("--lsof", action="store_true",
                   help="inspect per-process remote hosts (slower)")
    return p


def main():
    # enable UTF-8 so the box-drawing / particle glyphs render correctly
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    load_plugin_boards()           # register any driftboards/*.py plugins
    opts = build_parser().parse_args()
    tele = Telemetry(opts)
    tele.start()
    monitor = None
    if opts.global_keys:
        monitor = GlobalKeyMonitor()
        monitor.start()
    try:
        curses.wrapper(run_app, opts, tele, monitor)
    except KeyboardInterrupt:
        pass
    finally:
        tele.stop()
    print("\ndrift — see you out there. ✨\n")


if __name__ == "__main__":
    main()
