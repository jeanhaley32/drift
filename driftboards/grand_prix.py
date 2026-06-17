"""GRAND PRIX — a daily Formula-1 championship driftboard.

Each workday (day_start..day_end, local) is a Grand Prix: org members race down
a track by *work done* — commits authored in the configured private repos within
the window. Position = score, car speed = recent activity. At the day_end flag,
the top 10 bank F1 points (25-18-15…) into a persistent season championship.

Two views (auto-alternating): RACE (live track) and STANDINGS (season table).

Data: GitHub commits via `gh api` (uses your existing gh auth — no secret).
Scoped to the repos listed in config; commit-backboned so private work counts.

Manifest example:
  { "type": "grand_prix",
    "config": { "org": "your-org",
                "repos": ["your-org/backend", "your-org/frontend"],
                "day_start": "07:00", "day_end": "18:00", "tz": "America/New_York",
                "weights": {"commit": 1}, "points": [25,18,15,12,10,8,6,4,2,1],
                "exclude": ["dependabot[bot]"] } }
"""
import curses
import json
import os
import time
from datetime import datetime

from driftcore import (board, Scene, put, putch, center, clamp, lerp, run, cp,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE,
                       C_BLUE, REF_FPS)


def cp_(n):
    return cp(n)


def cp_bold(n):
    return cp(n) | curses.A_BOLD

CAR = "‹o═o›"                      # the chassis
POS_COLORS = {1: C_YELLOW, 2: C_WHITE, 3: C_RED}   # gold / silver / bronze-ish
SEASON_FILE = os.path.expanduser("~/.drift/championship.json")


def _tz(name):
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:                              # no tzdata / bad name
            pass
    return datetime.now().astimezone().tzinfo          # system local tz


def _hhmm(s, default):
    try:
        h, m = s.split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return default


@board
class GrandPrixBoard(Scene):
    name = "grand_prix"
    title = "▚ G R A N D   P R I X ▚"
    interval = 180.0                                   # refresh every 3 min
    CONFIG = {
        "org":       {"default": None},
        "repos":     {"default": []},
        "day_start": {"default": "07:00"},
        "day_end":   {"default": "18:00"},
        "tz":        {"default": None},
        "weights":   {"default": {"commit": 1}},
        "points":    {"default": [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]},
        "exclude":   {"default": []},
    }

    @classmethod
    def available(cls, tele, cfg=None):
        # needs gh auth + at least one repo configured
        return bool(getattr(tele, "gh_ok", False) and (cfg or {}).get("repos"))

    # ---- data (background thread) -----------------------------------------
    def _bounds(self, cfg):
        tz = _tz(cfg.get("tz"))
        now = datetime.now(tz)
        sh, sm = _hhmm(cfg.get("day_start"), (7, 0))
        eh, em = _hhmm(cfg.get("day_end"), (18, 0))
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        return now, start, end

    def _commits(self, repo, since_iso, until_iso, exclude):
        """logins -> commit count for one repo in the window."""
        out = run(["gh", "api", "--paginate",
                   f"repos/{repo}/commits?since={since_iso}&until={until_iso}"
                   "&per_page=100",
                   "--jq", '.[] | (.author.login // .commit.author.name // "unknown")'],
                  30)
        counts = {}
        for line in (out or "").splitlines():
            who = line.strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            counts[who] = counts.get(who, 0) + 1
        return counts

    def _prs_merged(self, repo, since_iso, until_iso, exclude):
        """logins -> count of PRs merged in the window for one repo."""
        q = f"repo:{repo} is:pr is:merged merged:{since_iso}..{until_iso}"
        out = run(["gh", "api", "--paginate", "-X", "GET", "search/issues",
                   "--field", f"q={q}", "--field", "per_page=100",
                   "--jq", '.items[] | (.user.login // "unknown")'], 30)
        counts = {}
        for line in (out or "").splitlines():
            who = line.strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            counts[who] = counts.get(who, 0) + 1
        return counts

    _REVIEW_Q = ("query($owner:String!,$name:String!){repository(owner:$owner,"
                 "name:$name){pullRequests(first:50,orderBy:{field:UPDATED_AT,"
                 "direction:DESC}){nodes{reviews(first:50){nodes{"
                 "author{login} submittedAt}}}}}}")

    def _reviews(self, repo, start, until, exclude):
        """logins -> count of PR reviews submitted in the window for one repo.
        Best-effort: scans the 50 most-recently-updated PRs (cheap, one call)."""
        try:
            owner, name = repo.split("/", 1)
        except ValueError:
            return {}
        out = run(["gh", "api", "graphql", "-f", f"query={self._REVIEW_Q}",
                   "-F", f"owner={owner}", "-F", f"name={name}", "--jq",
                   ".data.repository.pullRequests.nodes[].reviews.nodes[] | "
                   '[(.author.login // ""), .submittedAt] | @tsv'], 30)
        counts = {}
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            who, ts = parts[0].strip(), parts[1].strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start <= when <= until:
                counts[who] = counts.get(who, 0) + 1
        return counts

    def _season(self, points):
        """Load the season file and compute the standings table."""
        try:
            with open(SEASON_FILE) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            doc = {"races": {}}
        races = doc.get("races", {})
        agg = {}
        for _, race in races.items():
            order = race.get("order", [])
            won = race.get("fastest")
            for i, login in enumerate(order):
                a = agg.setdefault(login, {"login": login, "points": 0,
                                           "wins": 0, "podiums": 0})
                a["points"] += points[i] if i < len(points) else 0
                if i == 0:
                    a["wins"] += 1
                if i < 3:
                    a["podiums"] += 1
        table = sorted(agg.values(), key=lambda r: -r["points"])
        return doc, races, table

    def _bank(self, doc, key, order, fastest):
        doc.setdefault("races", {})[key] = {"order": order, "fastest": fastest}
        try:
            os.makedirs(os.path.dirname(SEASON_FILE), exist_ok=True)
            tmp = SEASON_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(doc, f, indent=2)
            os.replace(tmp, SEASON_FILE)
        except OSError:
            pass

    def fetch(self, cfg):
        now, start, end = self._bounds(cfg)
        points = cfg.get("points") or [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
        exclude = set(cfg.get("exclude") or [])
        weights = cfg.get("weights") or {}
        wc = float(weights.get("commit", 1))
        wp = float(weights.get("pr", 5))
        wr = float(weights.get("review", 2))
        state = "pre" if now < start else ("racing" if now < end else "done")

        drivers = []
        if state != "pre":
            until = min(now, end)
            sc, sp, sr = {}, {}, {}
            for repo in cfg.get("repos") or []:
                for who, n in self._commits(repo, start.isoformat(),
                                            until.isoformat(), exclude).items():
                    sc[who] = sc.get(who, 0) + n
                for who, n in self._prs_merged(repo, start.isoformat(),
                                               until.isoformat(), exclude).items():
                    sp[who] = sp.get(who, 0) + n
                for who, n in self._reviews(repo, start, until, exclude).items():
                    sr[who] = sr.get(who, 0) + n
            for w in set(sc) | set(sp) | set(sr):
                c, p, r = sc.get(w, 0), sp.get(w, 0), sr.get(w, 0)
                drivers.append({"login": w, "commits": c, "prs": p, "reviews": r,
                                "score": c * wc + p * wp + r * wr})
            drivers.sort(key=lambda d: -d["score"])

        # season: bank today's result once we're past the flag
        doc, races, _ = self._season(points)
        key = start.strftime("%Y-%m-%d")
        if state == "done" and key not in races and drivers:
            self._bank(doc, key, [d["login"] for d in drivers[:len(points)]],
                       drivers[0]["login"])
        _, races, table = self._season(points)

        span = (end - start).total_seconds()
        day_frac = clamp(((min(now, end) - start).total_seconds()) / span) \
            if span > 0 else (1.0 if now >= end else 0.0)

        return {"grand_prix": {
            "ok": True, "state": state,
            "drivers": drivers,
            "day_start": start.strftime("%H:%M"),
            "day_end": end.strftime("%H:%M"),
            "now": now.strftime("%H:%M"),
            "day_frac": day_frac,
            "secs_to_end": max(0, int((end - now).total_seconds())),
            "org": cfg.get("org") or "",
            "season_after": len(races),
            "season": table,
        }}

    # ---- rendering --------------------------------------------------------
    def build(self):
        self.t = 0.0
        self.disp = {}            # login -> displayed track fraction (smoothed)
        self.view_t = 0.0
        self.view = "race"
        self.prev_order = []
        self.flash = {}           # login -> overtake flash timer

    def update(self, dt, frame, st):
        self.t += dt
        self.view_t += dt
        if self.view_t > 14.0:    # alternate RACE / STANDINGS
            self.view_t = 0.0
            self.view = "standings" if self.view == "race" else "race"
        gp = st.get("grand_prix") or {}
        drivers = gp.get("drivers") or []
        lead = drivers[0]["score"] if drivers else 0.0
        order = [d["login"] for d in drivers]
        # overtake flashes
        for i, login in enumerate(order):
            pi = self.prev_order.index(login) if login in self.prev_order else i
            if pi > i:
                self.flash[login] = 1.0
        self.prev_order = order
        for k in list(self.flash):
            self.flash[k] = max(0.0, self.flash[k] - dt)
        # ease each car toward its score-based target fraction
        for d in drivers:
            target = (d["score"] / lead) if lead > 0 else 0.0
            cur = self.disp.get(d["login"], 0.0)
            self.disp[d["login"]] = cur + (target - cur) * min(1.0, dt * 1.5)

    def _flag(self, gp):
        return {"pre": ("◉ LIGHTS OUT — FORMATION LAP", C_RED),
                "racing": ("⚑ GREEN FLAG — RACING", C_GREEN),
                "done": ("🏁 CHEQUERED FLAG — RACE COMPLETE", C_WHITE)
                }.get(gp.get("state"), ("…", C_WHITE))

    def draw(self, scr, frame, st):
        gp = st.get("grand_prix")
        center(scr, 2, "GRAND PRIX", cp_bold(C_YELLOW))
        if not gp or not gp.get("ok"):
            if gp and gp.get("error"):
                center(scr, self.h // 2, f"telemetry error: {gp['error']}",
                       cp_(C_RED))
            else:
                center(scr, self.h // 2, "… waiting for the grid (gh) …",
                       cp_(C_YELLOW) | curses.A_DIM)
            return
        if self.view == "standings":
            self._draw_standings(scr, gp)
        else:
            self._draw_race(scr, gp)

    def _draw_race(self, scr, gp):
        org = gp.get("org", "")
        flag, fcol = self._flag(gp)
        # day clock — the race runs from start-of-day to the end-of-day flag
        dp = gp.get("day_frac", 0.0)
        barw = 16
        fill = int(dp * barw)
        bar = "▓" * fill + "░" * (barw - fill)
        center(scr, 3, f"{gp['day_start']}  [{bar}]  {gp['day_end']} 🏁   "
                       f"{int(dp * 100)}% of the day", cp_(C_CYAN))
        mm, ss = divmod(gp["secs_to_end"], 60)
        extra = (f"     ⏱ {mm // 60:02d}:{mm % 60:02d}:{ss:02d} to the flag"
                 if gp["state"] == "racing" else "")
        center(scr, 4, flag + extra, cp_bold(fcol))
        put(scr, 5, 3, f"{org}  ·  {len(gp.get('drivers') or [])} on track  "
                       f"·  scoring: commit×1  PR×5  review×2",
            cp_(C_WHITE) | curses.A_DIM)

        drivers = gp.get("drivers") or []
        if not drivers:
            msg = ("warming up — lights out at " + gp["day_start"]
                   if gp["state"] == "pre" else "no commits logged yet today")
            center(scr, self.h // 2, msg, cp_(C_YELLOW) | curses.A_DIM)
            return

        top = 6
        lane_h = 2
        n = min(len(drivers), max(1, (self.h - top - 3) // lane_h), 10)
        track_l, track_r = 9, self.w - 26
        lead = drivers[0]["score"]
        for i in range(n):
            d = drivers[i]
            y = top + i * lane_h
            pos = i + 1
            pcol = POS_COLORS.get(pos, C_WHITE)
            put(scr, y, 2, f"P{pos:<2}", cp_bold(pcol))
            # the track
            for x in range(track_l, track_r):
                putch(scr, y, x, "─" if x % 3 else "·", cp_(C_BLUE) | curses.A_DIM)
            putch(scr, y, track_r, "|", cp_(C_WHITE))     # finish line
            # the car (eased position)
            frac = clamp(self.disp.get(d["login"], 0.0))
            xcar = int(lerp(track_l, track_r - len(CAR), frac))
            moving = self.flash.get(d["login"], 0.0) > 0
            if xcar > track_l + 1:                     # exhaust / speed lines
                putch(scr, y, xcar - 1, "≈" if moving else "·",
                      cp_(C_YELLOW) | curses.A_DIM)
            carcol = C_GREEN if moving else pcol
            put(scr, y, xcar, CAR, cp_bold(carcol))
            # label + gap to leader
            gap = lead - d["score"]
            tag = (f"{d['login'][:16]}  {int(d['score'])}"
                   + ("" if i == 0 else f"  +{int(gap)}"))
            put(scr, y, track_r + 2, tag,
                cp_bold(pcol) if i < 3 else cp_(C_WHITE))
            if moving:
                put(scr, y, xcar - 9, "⚡OVERTAKE", cp_bold(C_YELLOW))

        lead_name = drivers[0]["login"]
        put(scr, self.h - 2, 3, f"P1 {lead_name} leads the Grand Prix",
            cp_bold(C_YELLOW))

    def _draw_standings(self, scr, gp):
        table = gp.get("season") or []
        center(scr, 3, f"DRIVERS' CHAMPIONSHIP   ·   after {gp['season_after']} races",
               cp_bold(C_CYAN))
        if not table:
            center(scr, self.h // 2,
                   "season starts after the first " + gp["day_end"] + " flag",
                   cp_(C_YELLOW) | curses.A_DIM)
            return
        top = 6
        maxp = table[0]["points"] or 1
        n = min(len(table), self.h - top - 2, 12)
        for i in range(n):
            r = table[i]
            y = top + i
            pos = i + 1
            pcol = POS_COLORS.get(pos, C_WHITE)
            barw = int((r["points"] / maxp) * max(6, self.w // 3))
            put(scr, y, 3, f"{pos:>2}  {r['login'][:18]:<18}", cp_bold(pcol))
            put(scr, y, 26, "█" * barw, cp_(pcol))
            put(scr, y, 26 + barw + 1,
                f"{r['points']} pts"
                + (f"  {r['wins']}×\U0001F3C6" if r["wins"] else ""),
                cp_(C_WHITE) | curses.A_DIM)

    def hud(self, st):
        gp = st.get("grand_prix") or {}
        drivers = gp.get("drivers") or []
        top = drivers[0] if drivers else None
        return [("grand prix", gp.get("org", "—")),
                ("flag", gp.get("state", "—")),
                ("day", f"{int(gp.get('day_frac', 0) * 100)}%  "
                        f"{gp.get('day_start','?')}→{gp.get('day_end','?')}"),
                ("on track", str(len(drivers))),
                ("leader", f"{top['login']} {int(top['score'])}" if top else "—"),
                ("P1 breakdown", f"c{top['commits']} p{top['prs']} r{top['reviews']}"
                 if top else "—")]
