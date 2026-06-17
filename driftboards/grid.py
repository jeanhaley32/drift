"""NET GRID — a cyberpunk data-tower skyline driftboard.

A neon skyline of the busiest sockets. Tower HEIGHT = a connection's activity
smoothed over a rolling time window (so heavy flows stay pinned and stable),
while ▲/▼ pulses climb each tower at its live up/down rate. Color = remote port.
"""
import curses
import random

from driftcore import (board, Scene, put, putch, center, cp, norm_log,
                       remote_port, PORT_COLOR, REF_FPS,
                       C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE)


@board
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
