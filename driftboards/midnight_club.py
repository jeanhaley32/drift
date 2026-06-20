"""MIDNIGHT CLUB — a leaderboard for the after-hours crew.

The Grand Prix celebrates the 9-to-5. The Midnight Club is its neon underground:
recognition for the work done *off the clock* — late nights and weekend nights,
on the empty highways while everyone else is asleep. No work-life balance, no
shame.

Off-hours = any time outside the day_start..day_end window, every day — exactly
the hours the Grand Prix doesn't count (the nightly gap; weekend daytime still
belongs to the Grand Prix, weekend nights are ours).

Two auto-rotating screens, both fed by one GitHub pull:
  TONIGHT  a live race of off-hours work since this evening's day_end flag.
  THE CLUB the rolling last-N-days collated respect standings.

"Respect" is off-hours GitHub work scored flat — raw grind, raw respect, no
diminishing returns: merged PRs ×5, opened ×3, reviews ×2, comments ×1, commits
×1.

Data: GitHub via `gh api` (your existing gh auth — no secret), scoped to the
configured repos. Computed live each refresh; nothing is persisted.

Manifest example:
  { "type": "midnight_club",
    "config": { "org": "your-org",
                "repos": ["your-org/backend", "your-org/frontend"],
                "day_start": "07:00", "day_end": "18:00", "tz": "America/New_York",
                "window_days": 7,
                "exclude": ["dependabot[bot]"] } }
"""
import curses
import random
from datetime import datetime, timedelta, timezone

from driftcore import (board, Scene, put, putch, center, clamp, lerp, run, cp,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE,
                       C_BLUE, REF_FPS)

NEON = [C_MAGENTA, C_CYAN, C_BLUE, C_GREEN, C_YELLOW, C_RED]
CAR = "▟██▙▸"                      # low-slung tuner, nose right
CAR_LEN = len(CAR)
# action -> (respect weight, breakdown field)
KIND = {"merged": (5, "prs_merged"), "open": (3, "prs_open"),
        "review": (2, "reviews"), "comment": (1, "review_comments"),
        "commit": (1, "commits")}


def _tz(name):
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def _hh(s, default):
    try:
        return int(s.split(":")[0])
    except (ValueError, AttributeError, IndexError):
        return default


def _parse(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _is_off(dt, tzinfo, sh, eh):
    """Off-hours = outside [sh, eh) in local time, any day (the Grand Prix
    complement)."""
    loc = dt.astimezone(tzinfo)
    return loc.hour < sh or loc.hour >= eh


def _lhash(login):
    """Stable deterministic hash of a login (independent of PYTHONHASHSEED)."""
    h = 0
    for ch in login or "?":
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h


def _color(login):
    """Fallback per-person color (one of the ~6 base neon colors) when the
    terminal can't do 256 — see MidnightClubScene._rc for the rich palette."""
    return NEON[_lhash(login) % len(NEON)]


@board
class MidnightClubScene(Scene):
    name = "midnight_club"
    title = "◐ M I D N I G H T   C L U B ◐"
    interval = 180.0
    CONFIG = {
        "org":         {"default": None},
        "repos":       {"default": []},
        "day_start":   {"default": "07:00"},
        "day_end":     {"default": "18:00"},
        "tz":          {"default": None},
        "window_days": {"default": 7},
        "weights":     {"default": {}},
        "exclude":     {"default": []},
        "max_racers":  {"default": 10},
    }

    @classmethod
    def available(cls, tele, cfg=None):
        return bool(getattr(tele, "gh_ok", False) and (cfg or {}).get("repos"))

    # ---- data (background thread): gather off-hours events with timestamps ---
    def _commit_events(self, repo, since, until, tzinfo, sh, eh, exclude):
        out = run(["gh", "api", "--paginate",
                   f"repos/{repo}/commits?since={since}&until={until}&per_page=100",
                   "--jq", '.[] | [(.author.login // ""), .commit.author.date] | @tsv'],
                  30)
        return self._events(out, tzinfo, sh, eh, exclude, "commit")

    def _pr_events(self, repo, field, rng, since, until, tzinfo, sh, eh, exclude):
        q = f"repo:{repo} is:pr {field} {rng}:{since}..{until}".replace("  ", " ")
        tsjq = ".created_at" if rng == "created" else ".pull_request.merged_at"
        out = run(["gh", "api", "--paginate", "-X", "GET", "search/issues",
                   "--field", f"q={q}", "--field", "per_page=100",
                   "--jq", f'.items[] | [(.user.login // ""), {tsjq}] | @tsv'], 30)
        return self._events(out, tzinfo, sh, eh, exclude,
                            "merged" if rng == "merged" else "open")

    def _events(self, out, tzinfo, sh, eh, exclude, kind):
        evs = []
        for line in (out or "").splitlines():
            p = line.split("\t")
            if len(p) != 2:
                continue
            who, ts = p[0].strip(), p[1].strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            dt = _parse(ts)
            if dt and _is_off(dt, tzinfo, sh, eh):
                evs.append((who, dt, kind))
        return evs

    _REVIEW_Q = ("query($owner:String!,$name:String!){repository(owner:$owner,"
                 "name:$name){pullRequests(first:50,orderBy:{field:UPDATED_AT,"
                 "direction:DESC}){nodes{reviews(first:50){nodes{"
                 "author{login} submittedAt state}}}}}}")
    _SUBSTANTIVE = {"APPROVED", "CHANGES_REQUESTED"}

    def _review_events(self, repo, since_dt, tzinfo, sh, eh, exclude):
        evs = []
        try:
            owner, name = repo.split("/", 1)
        except ValueError:
            return evs
        out = run(["gh", "api", "graphql", "-f", f"query={self._REVIEW_Q}",
                   "-F", f"owner={owner}", "-F", f"name={name}", "--jq",
                   ".data.repository.pullRequests.nodes[].reviews.nodes[] | "
                   '[(.author.login // ""), .submittedAt, .state] | @tsv'], 30)
        for line in (out or "").splitlines():
            p = line.split("\t")
            if len(p) != 3:
                continue
            who, ts, state = p[0].strip(), p[1].strip(), p[2].strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            dt = _parse(ts)
            if not dt or dt < since_dt or not _is_off(dt, tzinfo, sh, eh):
                continue
            if state in self._SUBSTANTIVE:
                evs.append((who, dt, "review"))
            elif state == "COMMENTED":
                evs.append((who, dt, "comment"))
        return evs

    def _board(self, events, weights, cap):
        agg = {}
        for who, _dt, kind in events:
            w, field = weights[kind]
            a = agg.get(who)
            if not a:
                a = agg[who] = {"login": who, "score": 0.0, "prs_merged": 0,
                                "prs_open": 0, "reviews": 0, "review_comments": 0,
                                "commits": 0}
            a[field] += 1
            a["score"] += w
        ranked = sorted(agg.values(), key=lambda r: -r["score"])
        return ranked[:cap], len(ranked)

    @staticmethod
    def _session(now_local, sh, eh):
        """(session_start_local, racing?) for 'tonight'. The night session runs
        from the most recent day_end flag; during work hours we're between
        sessions and show last night's run."""
        if now_local.hour >= eh:                       # this evening, live
            return now_local.replace(hour=eh, minute=0, second=0, microsecond=0), True
        if now_local.hour < sh:                        # small hours, still live
            y = now_local - timedelta(days=1)
            return y.replace(hour=eh, minute=0, second=0, microsecond=0), True
        # daytime: garage closed; show last night's completed run
        y = now_local - timedelta(days=1)
        return y.replace(hour=eh, minute=0, second=0, microsecond=0), False

    def fetch(self, cfg):
        repos = cfg.get("repos") or []
        if not repos:
            return {"midnight": {"error": "no repos configured"}}
        tzinfo = _tz(cfg.get("tz"))
        sh = _hh(cfg.get("day_start"), 7)
        eh = _hh(cfg.get("day_end"), 18)
        wd = max(1, int(cfg.get("window_days") or 7))
        exclude = set(cfg.get("exclude") or [])
        cap = int(cfg.get("max_racers") or 10)
        cw = cfg.get("weights") or {}
        weights = {k: (float(cw.get(_field_key(k), w)), field)
                   for k, (w, field) in KIND.items()}

        now_utc = datetime.now(timezone.utc)
        since_dt = now_utc - timedelta(days=wd)
        since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        until = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        events = []
        for repo in repos:
            events += self._commit_events(repo, since, until, tzinfo, sh, eh, exclude)
            events += self._pr_events(repo, "", "created", since, until, tzinfo, sh, eh, exclude)
            events += self._pr_events(repo, "is:merged", "merged", since, until, tzinfo, sh, eh, exclude)
            events += self._review_events(repo, since_dt, tzinfo, sh, eh, exclude)

        # the same events, bucketed into two windows
        now_local = now_utc.astimezone(tzinfo)
        sess_start, racing = self._session(now_local, sh, eh)
        tonight_events = [e for e in events if e[1] >= sess_start]

        week_racers, week_crew = self._board(events, weights, cap)
        tn_racers, tn_crew = self._board(tonight_events, weights, cap)

        # one shared, spread-out color per person across both screens
        cmap = self._assign_colors({r["login"] for r in week_racers}
                                   | {r["login"] for r in tn_racers})
        for r in week_racers:
            r["cidx"] = cmap.get(r["login"], 0)
        for r in tn_racers:
            r["cidx"] = cmap.get(r["login"], 0)

        return {"midnight": {
            "org": cfg.get("org") or "",
            "window": f"{sh:02d}:00–{eh:02d}:00",
            "tonight": {"racers": tn_racers, "crew": tn_crew, "racing": racing,
                        "since": f"{eh:02d}:00", "now": now_local.strftime("%H:%M")},
            "week": {"racers": week_racers, "crew": week_crew, "window_days": wd},
        }}

    # ---- render -----------------------------------------------------------
    def build(self):
        self.t = 0.0
        self.scroll = 0.0
        self.view_t = 0.0
        self.view_i = 0
        self.view = "tonight"
        self.disp = {}            # login -> eased track fraction (tonight race)
        self.skyline = self._make_skyline()

    _BLOCKS = " ▁▂▃▄▅▆▇█"

    def _make_skyline(self):
        # flat-topped buildings of varying height (in eighths) with sky gaps —
        # rendered as one clean silhouette row, not a filled band.
        cols = [0] * max(1, self.w)
        x = 0
        while x < self.w:
            x += random.randint(1, 4)                 # open sky between buildings
            bw, bh = random.randint(3, 9), random.randint(2, 7)
            for k in range(bw):
                if x + k < self.w:
                    cols[x + k] = bh
            x += bw
        return cols

    def resize(self, h, w):
        super().resize(h, w)
        self.skyline = self._make_skyline()

    # muted/dusky hues from the xterm-256 cube, hue-ordered (so palette-index
    # distance ≈ hue distance). Softer than the neon primaries but still diverse;
    # the assignment below spreads the on-screen crew far apart along this wheel.
    _PALETTE = [174, 173, 180, 179, 186, 143, 108, 114, 115, 116,
                110, 74, 103, 104, 140, 176, 175, 181]
    _PAIR_BASE = 40

    def _ensure_pairs(self):
        if getattr(self, "_pairs_ready", False):
            return
        self._pairs_ready = True
        self._use256 = False
        try:
            if curses.has_colors() and curses.COLORS >= 256:
                try:
                    curses.use_default_colors(); bg = -1
                except curses.error:
                    bg = curses.COLOR_BLACK
                for i, c in enumerate(self._PALETTE):
                    curses.init_pair(self._PAIR_BASE + i, c, bg)
                self._use256 = True
        except curses.error:
            self._use256 = False

    @classmethod
    def _assign_colors(cls, logins):
        """Deterministic, hash-seeded color assignment that spreads the crew
        across the hue wheel so no two blend. Each login prefers its hash slot;
        on a clash (or a too-close neighbor) it takes the free slot farthest — in
        circular palette distance — from those already taken. Same crew -> same
        colors; mostly stable as the crew changes."""
        P = len(cls._PALETTE)

        def circ(a, b):
            d = abs(a - b)
            return min(d, P - d)

        used, out = [], {}
        for lg in sorted(logins):
            pref = _lhash(lg) % P
            if not used:
                idx = pref
            elif pref not in used and min(circ(pref, u) for u in used) >= 2:
                idx = pref
            else:
                free = [c for c in range(P) if c not in used]
                idx = (max(free, key=lambda c: min(circ(c, u) for u in used))
                       if free else pref)
            used.append(idx)
            out[lg] = idx
        return out

    def _rc(self, cidx):
        """Color for a racer's assigned palette index, as a ready-to-OR attr."""
        if getattr(self, "_use256", False):
            return curses.color_pair(self._PAIR_BASE + (cidx % len(self._PALETTE)))
        return cp(NEON[cidx % len(NEON)])

    def update(self, dt, frame, st):
        self.t += dt
        self.scroll += dt * 22.0
        m = st.get("midnight") or {}
        avail = ["tonight"]
        if (m.get("week") or {}).get("racers"):
            avail.append("club")
        self.view_t += dt
        if self.view_t > 14.0:
            self.view_t = 0.0
            self.view_i += 1
        self.view = avail[self.view_i % len(avail)]
        # ease cars toward tonight's standing
        racers = (m.get("tonight") or {}).get("racers") or []
        lead = racers[0]["score"] if racers else 0.0
        for r in racers:
            target = (r["score"] / lead) if lead > 0 else 0.0
            cur = self.disp.get(r["login"], 0.0)
            self.disp[r["login"]] = cur + (target - cur) * min(1.0, dt * 1.5)

    def _draw_skyline(self, scr):
        # one clean neon silhouette on row 2: partial blocks set each building's
        # height, gaps are open sky, a few towers tinted neon. Clear of the title.
        for x, h in enumerate(self.skyline):
            if x >= self.w or h <= 0:
                continue
            col = (C_MAGENTA if (x // 11) % 5 == 0
                   else (C_CYAN if (x // 7) % 4 == 0 else C_BLUE))
            putch(scr, 2, x, self._BLOCKS[min(8, h)], cp(col) | curses.A_DIM)

    def draw(self, scr, frame, st):
        self._ensure_pairs()
        m = st.get("midnight")
        center(scr, 0, "🌃  M I D N I G H T   C L U B  🌃", cp(C_MAGENTA) | curses.A_BOLD)
        if not m or m.get("error"):
            msg = (m or {}).get("error") or "rolling out to the strip…"
            center(scr, self.h // 2, msg, cp(C_CYAN) | curses.A_DIM)
            return
        self._draw_skyline(scr)
        if self.view == "club":
            self._draw_club(scr, m)
        else:
            self._draw_tonight(scr, m)

    def _draw_tonight(self, scr, m):
        t = m.get("tonight") or {}
        racing = t.get("racing")
        if racing:
            hdr = f"🌙 TONIGHT'S RACE · lights on since {t.get('since','?')} · now {t.get('now','?')}"
            hc = C_GREEN
        else:
            hdr = f"☾ garage closed · last night's run · racing resumes after {m.get('window','?').split('–')[-1]}"
            hc = C_YELLOW
        center(scr, 3, hdr, cp(hc) | curses.A_DIM)
        racers = t.get("racers") or []
        if not racers:
            msg = ("the streets are empty — nobody out yet tonight" if racing
                   else "no off-hours runs last night")
            center(scr, self.h // 2, msg, cp(C_CYAN) | curses.A_DIM)
            return

        top = 5
        lane_h = 2
        cap = int(self.cfg.get("max_racers") or 10)
        fit = (self.h - top - 4) // (lane_h + 1)
        show_card = fit >= 2
        n = (min(len(racers), cap, max(1, fit)) if show_card
             else min(len(racers), cap, max(1, (self.h - top - 2) // lane_h)))
        track_l, track_r = 10, self.w - 24
        lead = racers[0]["score"]
        for i in range(n):
            r = racers[i]
            y = top + i * lane_h
            rc = self._rc(r.get("cidx", 0))
            put(scr, y, 2, f"#{i + 1:<2}", rc | curses.A_BOLD)
            for x in range(track_l, track_r):
                if (x + int(self.scroll)) % 6 < 2:
                    putch(scr, y, x, "─", cp(C_BLUE) | curses.A_DIM)
            frac = clamp(self.disp.get(r["login"], 0.0))
            xcar = int(lerp(track_l, track_r - CAR_LEN, frac))
            for j, ch in enumerate(("▒", "░", "·"), start=1):
                gx = xcar - j
                if gx <= track_l:
                    break
                putch(scr, y, gx, ch, cp(C_CYAN if j % 2 else C_MAGENTA) | curses.A_DIM)
            put(scr, y, xcar, CAR, rc | curses.A_BOLD)
            putch(scr, y, xcar + CAR_LEN - 1, "▸", cp(C_YELLOW) | curses.A_BOLD)
            gap = lead - r["score"]
            tag = (f"{r['login'][:12]}  {int(r['score'])}"
                   + ("" if i == 0 else f"  -{int(gap)}"))
            put(scr, y, track_r + 2, tag, rc | curses.A_BOLD)

        if show_card:
            self._draw_dossier(scr, top + n * lane_h + 1, racers, n,
                               "TONIGHT'S RAP SHEET")
        leader = racers[0]["login"]
        tail = ("owns the streets tonight" if racing else "took last night")
        put(scr, self.h - 1, 3,
            f"#1 {leader} {tail}  ·  {t.get('crew', len(racers))} out",
            cp(C_MAGENTA) | curses.A_BOLD)

    def _draw_club(self, scr, m):
        w = m.get("week") or {}
        racers = w.get("racers") or []
        center(scr, 3, f"THE CLUB · last {w.get('window_days', 7)} days collated · "
                       f"off-hours respect", cp(C_CYAN) | curses.A_DIM)
        if not racers:
            center(scr, self.h // 2, "nobody's been grinding off-hours this week",
                   cp(C_CYAN) | curses.A_DIM)
            return
        top = 5
        stride = 2                                    # blank row between entries
        maxp = racers[0]["score"] or 1
        cap = int(self.cfg.get("max_racers") or 10)
        n = min(len(racers), cap, max(1, (self.h - top - 1) // stride))
        bar_max = max(8, self.w // 3)
        for i in range(n):
            r = racers[i]
            y = top + i * stride
            rc = self._rc(r.get("cidx", 0))                 # the racer is their car/color
            put(scr, y, 3, f"#{i + 1:<2}", rc | curses.A_BOLD)
            put(scr, y, 6, CAR, rc | curses.A_BOLD)               # represented by their car
            put(scr, y, 12, r["login"][:16], rc | curses.A_BOLD)
            barw = int((r["score"] / maxp) * bar_max)
            put(scr, y, 30, "█" * barw, rc)
            detail = (f"{int(r['score'])} resp   "
                      f"m{r['prs_merged']} o{r['prs_open']} "
                      f"rev{r['reviews']}+{r['review_comments']} c{r['commits']}")
            put(scr, y, 30 + barw + 1, detail, cp(C_WHITE) | curses.A_DIM)
        put(scr, self.h - 1, 3,
            f"{w.get('crew', len(racers))} in the club  ·  no work-life balance, all respect",
            cp(C_MAGENTA) | curses.A_BOLD)

    _DOSSIER_COLS = (("#", 3), ("RIDE", 6), ("RACER", 13), ("MRG", 30),
                     ("OPN", 36), ("REV", 42), ("COM", 48), ("CMT", 54),
                     ("RESP", 61))

    def _draw_dossier(self, scr, y0, racers, n, title):
        if y0 >= self.h - 2:
            return
        put(scr, y0, 3, f"── {title} " + "─" * 8, cp(C_MAGENTA) | curses.A_DIM)
        put(scr, y0 + 1, 3,
            "respect = off-hours only ·  merged ×5  opened ×3  review ×2  "
            "comment ×1  commit ×1", cp(C_CYAN) | curses.A_DIM)
        hy = y0 + 2
        for label, x in self._DOSSIER_COLS:
            put(scr, hy, x, label, cp(C_WHITE) | curses.A_DIM)
        for i in range(n):
            ry = hy + 1 + i
            if ry >= self.h - 1:
                break
            r = racers[i]
            rc = self._rc(r.get("cidx", 0))                 # this racer's permanent color
            put(scr, ry, 3, f"{i + 1:>2}", rc | curses.A_BOLD)
            put(scr, ry, 6, CAR, rc | curses.A_BOLD)              # showing off the ride
            put(scr, ry, 13, r["login"][:16], rc | curses.A_BOLD)
            for x, v in ((30, r["prs_merged"]), (36, r["prs_open"]),
                         (42, r["reviews"]), (48, r["review_comments"]),
                         (54, r["commits"])):
                put(scr, ry, x, f"{v:>3}", cp(C_WHITE))
            put(scr, ry, 61, f"{int(r['score']):>4}", rc | curses.A_BOLD)

    def hud(self, st):
        m = st.get("midnight") or {}
        tn = (m.get("tonight") or {})
        wk = (m.get("week") or {})
        tr = (tn.get("racers") or [])
        wr = (wk.get("racers") or [])
        return [("midnight club", m.get("org", "—")),
                ("off-hours", f"outside {m.get('window','?')}"),
                ("tonight", ("RACING" if tn.get("racing") else "garage")
                            + f" · {tn.get('crew', 0)} out"),
                ("tonight P1", f"{tr[0]['login']} {int(tr[0]['score'])}" if tr else "—"),
                (f"club ({wk.get('window_days', 7)}d) P1",
                 f"{wr[0]['login']} {int(wr[0]['score'])}" if wr else "—")]


def _field_key(kind):
    # manifest weight keys mirror Grand Prix's: pr_merged / pr_open / review / …
    return {"merged": "pr_merged", "open": "pr_open", "review": "review",
            "comment": "review_comment", "commit": "commit"}[kind]
