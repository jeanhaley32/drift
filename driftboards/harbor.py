"""HARBOR — your Docker fleet as ships in an animated harbor.

Each container is a ship: cargo stack = memory, funnel smoke = CPU, wake = net
I/O, crew count in the label = PIDs, hull color = state. Ships rise in when a
container starts and sink when one stops; the Docker whale cruises through now
and then.
"""
import curses
import random

from driftcore import (board, Scene, put, putch, center, cp, norm_log, REF_FPS,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE,
                       C_BLUE)

HULL_W = 11
SHIP_COL = HULL_W + 5
WHALE = ["  [#][#][#]", "<(___________)", "  \\~~~~~~~~~/"]
CARGO_COLORS = [C_CYAN, C_YELLOW, C_MAGENTA, C_GREEN]


@board
class HarborScene(Scene):
    """Each Docker container is a ship: cargo stack = memory, funnel smoke =
    CPU, wake = network I/O, crew count in the label = PIDs, hull color = state.
    Ships rise in when a container starts and sink when one stops; the Docker
    whale cruises through now and then."""
    name = "harbor"
    title = "⚓ H A R B O R ⚓"

    @classmethod
    def available(cls, tele, cfg=None):
        return bool(getattr(tele, "docker_ok", False))

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
