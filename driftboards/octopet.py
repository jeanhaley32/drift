"""OCTO-PET — a Tamagotchi whose mood reflects your GitHub activity.

A little ASCII companion that lives off your GitHub activity: a long contribution
streak keeps it energetic and happy, idleness makes it sleepy/hungry. It's
orbited by your ★ stars, ♥ followers, and ! notifications, carries your open
PRs, and chomps commit pellets when you've been active.
"""
import curses
import math
import random

from driftcore import (board, Scene, put, putch, center, cp, clamp, REF_FPS,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE,
                       C_BLUE)


@board
class OctoPetScene(Scene):
    """A little ASCII companion that lives off your GitHub activity: a long
    contribution streak keeps it energetic and happy, idleness makes it sleepy/
    hungry; it's orbited by your ★ stars, ♥ followers, and ! notifications,
    carries your open PRs, and chomps commit pellets when you've been active."""
    name = "octopet"
    title = "◓ O C T O - P E T ◓"

    @classmethod
    def available(cls, tele, cfg=None):
        return bool(getattr(tele, "gh_ok", False))

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
