"""SIGNAL — a live Wi-Fi neighborhood radar driftboard.

The network you're connected to sits at the hub; every nearby network is a spoke
whose distance from the hub tracks its signal strength (stronger = closer),
labeled with its real name. A radar sweep rotates around and pings each node as
it passes; signal motes drift inward along the spokes.
"""
import curses
import math
import random

from driftcore import (board, Scene, put, putch, plot_line, cp, lerp,
                       rssi_to_frac, make_stars, draw_stars, REF_FPS,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE,
                       C_BLUE)


@board
class SignalScene(Scene):
    name = "signal"
    title = "S I G N A L"

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
