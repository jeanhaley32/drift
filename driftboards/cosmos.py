"""COSMOS — retro-space driftboard.

Meteors fall at your download rate, a rocket climbs with CPU load, latency pulses
ripple from a central beacon, nearby Wi-Fi networks twinkle as a labeled star
cluster, and a UFO drifts by. Pure telemetry-driven ambient space.
"""
import curses
import math
import random

from driftcore import (board, Scene, put, putch, cp, clamp, rssi_to_frac,
                       make_stars, draw_stars, REF_FPS,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE)

PLANET_ART = ["  .--.  ", "-( ~~ )-", "  '--'  "]
ROCKET_ART = [" /\\ ", "|=*|", "|MB|", "/__\\"]
UFO_ART = [" .-=-. ", "(o-o-o)"]


@board
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
                ("pulse = latency", f"{st['latency']:.0f} ms" if st['latency'] is not None else "—"),
                ("rocket = cpu", f"{st['cpu']*100:.0f}%"),
                ("stars(L) = nearby wifi", f"{len(st.get('neighbors') or [])} nets")]
