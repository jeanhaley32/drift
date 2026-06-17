"""SOCKETS — a live patch-bay of individual network connections.

One row per real socket (via nettop); each row's scrolling glyphs are driven by
THAT connection's actual download/upload bytes-per-second, and the row color is
keyed to the remote port (443/80/53/22…).
"""
import curses

from driftcore import (board, Scene, put, putch, center, cp, norm_log,
                       remote_port, PORT_COLOR,
                       C_GREEN, C_YELLOW, C_CYAN, C_WHITE)


@board
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
