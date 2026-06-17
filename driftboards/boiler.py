"""BOILER ROOM — steampunk machine-room driftboard.

Semicircular pressure gauges read CPU / MEM / NET, gears and pistons spin faster
under CPU load, steam puffs when memory is high, and a CPU spike pops an
over-pressure relief flash. A nameplate calls out the current throughput hog.
"""
import curses
import math
import random

from driftcore import (board, Scene, put, putch, center, plot_line, cp, clamp,
                       REF_FPS, C_RED, C_GREEN, C_YELLOW, C_CYAN, C_WHITE)


@board
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
