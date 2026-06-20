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


# PORT_COLOR is a shared constant used by SOCKETS and NET GRID (both plugins).
PORT_COLOR = {443: C_GREEN, 80: C_YELLOW, 53: C_CYAN, 5353: C_CYAN,
              22: C_MAGENTA, 5223: C_BLUE}


# ============================================================================
# THEME : HARBOR  (Docker containers as ships in an animated harbor)
# (COSMOS, BOILER, SIGNAL, SOCKETS, GRID have migrated to driftboards/)
# ============================================================================
# All boards now live in driftboards/ as @board-registered plugins (loaded by
# load_plugin_boards). Each declares its own availability via available(), so
# there is no built-in THEMES list or hardcoded gating here anymore.


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
    rot = dict(doc.get("rotation", {}))
    layout = doc.get("layout", {})
    if isinstance(layout, dict) and "tiles" in layout:
        rot["tiles"] = layout["tiles"]
    return entries, rot


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
    tag = " drift · q quit · space skip · n/p board · 1-4 tiles · l lock · h hud · f fps "
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
# Tiling: a Viewport wraps the real screen with an offset + clip, so a board's
# put/putch/center (which all go through scr.getmaxyx/addstr/addch) render into
# one tile thinking they own a full screen of the tile's size — no board changes.
# ----------------------------------------------------------------------------
class Viewport:
    def __init__(self, scr, y0, x0, h, w):
        self._scr = scr
        self._y0, self._x0, self._h, self._w = y0, x0, h, w

    def getmaxyx(self):
        return (self._h, self._w)

    def addstr(self, y, x, text, attr=0):
        if 0 <= y < self._h and x < self._w:
            try:
                self._scr.addstr(self._y0 + y, self._x0 + x, text, attr)
            except curses.error:
                pass

    def addch(self, y, x, ch, attr=0):
        if 0 <= y < self._h and 0 <= x < self._w:
            try:
                self._scr.addch(self._y0 + y, self._x0 + x, ch, attr)
            except curses.error:
                pass

    def erase(self):
        pass                       # the parent screen is erased once per frame


def tile_rects(h, w, tiles):
    """Lay out 1–4 tiles as (y0, x0, h, w) rects. 2 = side by side, 3 = one big
    panel + two stacked, 4 = quadrants."""
    if tiles <= 1:
        return [(0, 0, h, w)]
    if tiles == 2:
        wl = w // 2
        return [(0, 0, h, wl), (0, wl, h, w - wl)]
    if tiles == 3:
        wl, ht = w // 2, h // 2
        return [(0, 0, h, wl), (0, wl, ht, w - wl), (ht, wl, h - ht, w - wl)]
    wl, ht = w // 2, h // 2
    return [(0, 0, ht, wl), (0, wl, ht, w - wl),
            (ht, 0, h - ht, wl), (ht, wl, h - ht, w - wl)]


def tile_border(vp, title, focused=False, locked=False):
    """A light box + title for one tile. The focused (selected) tile gets a
    bright border; a locked tile shows a 🔒 and won't rotate."""
    h, w = vp.getmaxyx()
    a = (cp(C_CYAN) | curses.A_BOLD) if focused else (cp(C_BLUE) | curses.A_DIM)
    for x in range(w):
        putch(vp, 0, x, "─", a)
        putch(vp, h - 1, x, "─", a)
    for y in range(h):
        putch(vp, y, 0, "│", a)
        putch(vp, y, w - 1, "│", a)
    label = (" 🔒 " if locked else " ") + title + " "
    put(vp, 0, 2, label, cp((C_CYAN if focused else C_WHITE)) | curses.A_BOLD)


def move_focus(cur, dirn, rects):
    """Pick the tile nearest to `cur` in a screen direction ('L','R','U','D'),
    by tile centers — works for any of the 1–4 layouts. Stays put if none."""
    y0, x0, th, tw = rects[cur]
    cy, cx = y0 + th / 2, x0 + tw / 2
    best, bestd = cur, None
    for j, (jy0, jx0, jh, jw) in enumerate(rects):
        if j == cur:
            continue
        jy, jx = jy0 + jh / 2, jx0 + jw / 2
        if dirn == "L" and jx >= cx:
            continue
        if dirn == "R" and jx <= cx:
            continue
        if dirn == "U" and jy >= cy:
            continue
        if dirn == "D" and jy <= cy:
            continue
        d = (abs(jx - cx) + abs(jy - cy) * 3 if dirn in ("L", "R")
             else abs(jy - cy) + abs(jx - cx) * 3)
        if bestd is None or d < bestd:
            best, bestd = j, d
    return best


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
        # every available board in the registry (built-ins + migrated plugins),
        # no config — drift's original "rotate through everything" behavior
        for cls in BOARDS.values():
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
    kacc = 0              # keystrokes seen this frame
    sparks = []           # typing-energy particles: [x,y,vy,vx,char,color]
    speed = 1.0

    h, w = scr.getmaxyx()
    # rotation pool of board specs (from manifest, or all available built-ins)
    specs, rotation = resolve_boards(opts, tele)
    start_fetchers(specs, tele)          # background data pulls for fetch boards
    min_secs = rotation.get("min_secs", opts.min_secs)
    max_secs = rotation.get("max_secs", opts.max_secs)
    lock_all = rotation.get("lock", opts.lock)     # freeze every panel's rotation

    # how many panels to tile (CLI > manifest layout.tiles > 1)
    tiles = max(1, min(4, opts.tiles or int(rotation.get("tiles") or 1)))
    theme_lock = None
    if opts.theme and opts.theme in BOARDS:
        tiles, lock_all = 1, True
        theme_lock = next((i for i, s in enumerate(specs)
                           if s["cls"].name == opts.theme), None)
        if theme_lock is None:
            specs.append({"cls": BOARDS[opts.theme], "cfg": {}, "label": opts.theme})
            theme_lock = len(specs) - 1

    rects = tile_rects(h, w, tiles)

    def pick(used, own):
        """A spec index not shown in another tile (and, if possible, not `own`)."""
        ch = [i for i in range(len(specs)) if i not in used and i != own]
        if not ch:
            ch = ([i for i in range(len(specs)) if i not in used]
                  or [i for i in range(len(specs)) if i != own]
                  or list(range(len(specs))))
        return random.choice(ch)

    def new_slots(n):
        sl, used = [], set()
        for r in range(n):
            si = theme_lock if (theme_lock is not None and r == 0) else pick(used, None)
            used.add(si)
            sl.append({"i": si, "locked": False,
                       "scene": make_board(specs[si], rects[r][2], rects[r][3], tele),
                       "seg": random.uniform(min_secs, max_secs), "start": time.time()})
        return sl

    def resize_slot(j):
        slots[j]["scene"].resize(rects[j][2], rects[j][3])

    def set_board(j, ni):
        slots[j]["i"] = ni
        slots[j]["scene"] = make_board(specs[ni], rects[j][2], rects[j][3], tele)
        slots[j]["start"] = time.time()

    slots = new_slots(tiles)
    focus = 0             # the tile keys act on (Tab to move)
    last = time.time()
    aframe = 0.0          # animation clock in REF_FPS "frames" (fps-independent)
    force_all = False     # space: bring a new board to every unlocked panel now
    end_at = (time.time() + opts.minutes * 60) if opts.minutes else None

    while True:
        now = time.time()
        raw_dt = now - last                       # real frame interval
        dt = min(0.2, raw_dt) * speed
        last = now
        frames += 1
        if raw_dt > 0:                            # smoothed measured fps (EMA)
            fps_meas += (1.0 / raw_dt - fps_meas) * 0.1
        aframe += dt * REF_FPS
        frame = int(aframe)

        # resize
        nh, nw = scr.getmaxyx()
        if (nh, nw) != (h, w):
            h, w = nh, nw
            rects = tile_rects(h, w, tiles)
            for r, slot in enumerate(slots):
                slot["scene"].resize(rects[r][2], rects[r][3])

        # input
        k = scr.getch()
        while k != -1:
            if k != curses.KEY_RESIZE:
                kacc += 1
            if k in (ord("q"), ord("Q")):
                return
            elif k == ord(" "):
                force_all = True            # bring new boards to unlocked panels
            elif k in (ord("1"), ord("2"), ord("3"), ord("4")) and theme_lock is None:
                tiles = k - ord("0")
                rects = tile_rects(h, w, tiles)
                slots = new_slots(tiles)
                focus = 0
            # --- select the tile to control (arrow keys / Tab highlight it) ---
            elif k == curses.KEY_LEFT:
                focus = move_focus(focus, "L", rects)
            elif k == curses.KEY_RIGHT:
                focus = move_focus(focus, "R", rects)
            elif k == curses.KEY_UP:
                focus = move_focus(focus, "U", rects)
            elif k == curses.KEY_DOWN:
                focus = move_focus(focus, "D", rects)
            elif k in (ord("\t"), 9):
                focus = (focus + 1) % len(slots)
            # --- act on the focused tile ---
            elif k == ord("l") and theme_lock is None:      # lock/unlock this tile
                slots[focus]["locked"] = not slots[focus].get("locked")
            elif k == ord("L"):                              # lock/unlock ALL panels
                lock_all = not lock_all
            elif k in (ord("n"), ord("p")) and theme_lock is None:
                # next / prev dashboard for the focused panel (skip ones shown elsewhere)
                used = {s["i"] for j, s in enumerate(slots) if j != focus}
                step = 1 if k == ord("n") else -1
                ni, guard = (slots[focus]["i"] + step) % len(specs), 0
                while ni in used and len(specs) > len(slots) and guard < len(specs):
                    ni = (ni + step) % len(specs); guard += 1
                set_board(focus, ni)
            elif k in (ord(","), ord("<")) and theme_lock is None:
                j = (focus - 1) % len(slots)                # move panel left (swap)
                slots[focus], slots[j] = slots[j], slots[focus]
                resize_slot(focus); resize_slot(j); focus = j
            elif k in (ord("."), ord(">")) and theme_lock is None:
                j = (focus + 1) % len(slots)                # move panel right (swap)
                slots[focus], slots[j] = slots[j], slots[focus]
                resize_slot(focus); resize_slot(j); focus = j
            elif k in (ord("s"), ord("S")) and theme_lock is None:
                # shuffle the unlocked panels' positions among themselves
                mv = [j for j in range(len(slots)) if not slots[j].get("locked")]
                shuffled = [slots[j] for j in mv]
                random.shuffle(shuffled)
                for pos, j in zip(mv, shuffled):
                    slots[pos] = j
                for j in mv:
                    resize_slot(j)
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

        base_st = tele.snapshot()
        base_st["typing"] = intensity
        base_st["kps"] = kps

        # per-panel rotation: each tile runs its own timer and never shows a board
        # already on screen in another tile
        for idx, slot in enumerate(slots):
            if slot.get("locked"):
                continue                          # this tile is locked in place
            expired = (now - slot["start"]) >= slot["seg"]
            if not (force_all or (not lock_all and expired)):
                continue
            if theme_lock is not None and idx == 0:
                continue                          # the --theme board stays put
            used = {s["i"] for j, s in enumerate(slots) if j != idx}
            ni = pick(used, slot["i"])
            if tiles == 1 and not force_all:       # keep the solo-board dissolve
                if not transition(scr, h, w, specs[ni]["cls"].title, frame):
                    return
                now = last = time.time()
            slot["i"] = ni
            slot["scene"] = make_board(specs[ni], rects[idx][2], rects[idx][3], tele)
            slot["seg"] = random.uniform(min_secs, max_secs)
            slot["start"] = time.time()
        force_all = False

        # draw every panel into its viewport
        scr.erase()
        for idx, slot in enumerate(slots):
            y0, x0, th, tw = rects[idx]
            vp = Viewport(scr, y0, x0, th, tw)
            data = specs[slot["i"]].get("data")
            st = dict(base_st)
            if data:
                st.update(data)
            slot["scene"].update(dt, frame, st)
            slot["scene"].draw(vp, frame, st)
            if tiles == 1:
                draw_border(scr, h, w, slot["scene"].title)
            else:
                tile_border(vp, slot["scene"].title,
                            focused=(idx == focus), locked=slot.get("locked"))
        # typing-energy sparks (global overlay over all panels)
        for s in sparks:
            bright = curses.A_BOLD if s[1] > h * 0.5 else curses.A_DIM
            putch(scr, s[1], s[0], s[4], cp(s[5]) | bright)
        # decoder HUD only when a single board fills the screen
        if show_hud and tiles == 1:
            s0 = slots[0]
            hst = dict(base_st, **(specs[s0["i"]].get("data") or {}))
            draw_hud(scr, h, w, s0["scene"], hst)
        if show_fps:
            wpm = int(kps * 12)                          # ~5 chars/word
            kico = "⌨⚡" if (monitor and monitor.ok) else "⌨"
            tlabel = f"{tiles} tile" + ("s" if tiles > 1 else "")
            meter = f" {fps_meas:4.1f} fps · {tlabel} · {kico} {kps:4.1f}/s {wpm} wpm "
            col = C_GREEN if fps_meas >= fps * 0.9 else (
                C_YELLOW if fps_meas >= fps * 0.6 else C_RED)
            put(scr, h - 1, 3, meter, cp(col) | curses.A_BOLD)
        if tiles > 1:                                    # global controls hint
            tag = (" ←↑→↓ select · l lock · n/p board · </> move · "
                   "s shuffle · 1-4 tiles · q ")
            put(scr, h - 1, max(3, w - len(tag) - 2), tag, cp(C_BLUE) | curses.A_DIM)
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
    p.add_argument("--tiles", type=int, default=0, choices=[0, 1, 2, 3, 4],
                   help="show N driftboards tiled at once (1–4; also keys 1-4 "
                        "in-app). Default: manifest layout.tiles, else 1")
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
