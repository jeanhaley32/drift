"""GITHUB — the live contribution heatmap + account stats driftboard.

Renders the past-year contribution calendar as an animated heatmap with a panel
of account stats (followers, repos, stars, open PRs, notifications, streaks).
A shimmer sweeps across the grid; today's cell pulses.
"""
import curses

from driftcore import (board, Scene, put, putch, center, cp,
                       C_GREEN, C_YELLOW, C_WHITE)

# intensity level -> (glyph, color, attr)
GH_LEVELS = [("·", C_WHITE, curses.A_DIM), ("░", C_GREEN, 0),
             ("▒", C_GREEN, 0), ("▓", C_GREEN, curses.A_BOLD),
             ("█", C_GREEN, curses.A_BOLD)]
GH_DOW = {1: "Mon", 3: "Wed", 5: "Fri"}


@board
class GithubScene(Scene):
    """Renders the live GitHub contribution calendar as an animated heatmap,
    with a panel of account stats (followers, repos, stars, PRs, notifications,
    streaks). A shimmer sweeps across the grid; today's cell pulses."""
    name = "github"
    title = "▦ G I T H U B ▦"

    @classmethod
    def available(cls, tele, cfg=None):
        return bool(getattr(tele, "gh_ok", False))

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
