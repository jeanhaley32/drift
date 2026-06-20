"""GRAND PRIX — a sprint-long Formula-1 championship driftboard.

Each two-week SPRINT is a named Grand Prix (auto-cycling F1 circuits — Monaco,
Monza, … — plus a sprint number). drift counts its own sprints from 1 on a fixed
cadence: sprint_anchor is the start date of Sprint 1, and every sprint_days the
counter advances. (Set sprint_anchor to a real sprint start so the cadence lines
up with your team's sprints — drift doesn't read Linear at runtime.)

Every DAY of the sprint is one race: during the workday window (day_start..
day_end) org members race down the track by *work done* in the configured repos,
scored by a diminishing-returns rubric (merged PRs, opened PRs, reviews,
commits). At the day_end flag the finishing order banks F1 points (25-18-15…).
Those daily points add up into the sprint's Grand Prix scoreboard, and each
sprint's GP result rolls into a year-long Drivers' Championship.

Three auto-rotating views: RACE (today, live) · SPRINT (this GP's board) ·
SEASON (the championship across all sprints).

Data: GitHub via `gh api` (your existing gh auth — no secret), scoped to the
configured repos. Results persist in ~/.drift/championship.json.

Manifest example:
  { "type": "grand_prix",
    "config": { "org": "your-org",
                "repos": ["your-org/backend", "your-org/frontend"],
                "day_start": "07:00", "day_end": "18:00", "tz": "America/New_York",
                "sprint_anchor": "2026-06-15", "sprint_days": 14,
                "points": [25,18,15,12,10,8,6,4,2,1],
                "exclude": ["dependabot[bot]"] } }
"""
import curses
import json
import os
import time
from datetime import datetime, date, timedelta

from driftcore import (board, Scene, put, putch, center, clamp, lerp, run, cp,
                       C_RED, C_GREEN, C_YELLOW, C_CYAN, C_MAGENTA, C_WHITE,
                       C_BLUE, REF_FPS)


def cp_(n):
    return cp(n)


def cp_bold(n):
    return cp(n) | curses.A_BOLD

CAR_LEN = 5                        # chassis is "‹o═o▸" — tail, 2 wheels, nose
WHEELS = ("◍", "◉")               # alternated per frame to look like rolling


def car_sprite(frame, moving):
    """The chassis with rolling wheels: ‹◍═◍▸ / ‹◉═◉▸. Wheels spin faster when
    the car is moving up the order."""
    w = WHEELS[(frame // (2 if moving else 4)) % 2]
    return f"‹{w}═{w}▸"
POS_COLORS = {1: C_YELLOW, 2: C_WHITE, 3: C_RED}   # gold / silver / bronze-ish
SEASON_FILE = os.path.expanduser("~/.drift/championship.json")

# One F1-style circuit per sprint, cycling — (flag, name). Overridable via the
# "circuits" config (a list of names, or of [flag, name] pairs).
CIRCUITS = [("🇧🇭", "Bahrain"), ("🇸🇦", "Jeddah"), ("🇦🇺", "Melbourne"),
            ("🇯🇵", "Suzuka"), ("🇨🇳", "Shanghai"), ("🇺🇸", "Miami"),
            ("🇮🇹", "Imola"), ("🇲🇨", "Monaco"), ("🇨🇦", "Montreal"),
            ("🇪🇸", "Barcelona"), ("🇦🇹", "Spielberg"), ("🇬🇧", "Silverstone"),
            ("🇭🇺", "Budapest"), ("🇧🇪", "Spa"), ("🇳🇱", "Zandvoort"),
            ("🇮🇹", "Monza"), ("🇦🇿", "Baku"), ("🇸🇬", "Singapore"),
            ("🇺🇸", "Austin"), ("🇲🇽", "Mexico City"), ("🇧🇷", "Interlagos"),
            ("🇺🇸", "Las Vegas"), ("🇶🇦", "Lusail"), ("🇦🇪", "Abu Dhabi")]


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


def _fmt_date(d):
    return d.strftime("%b ") + str(d.day)              # "Jun 15", no zero-pad


@board
class GrandPrixBoard(Scene):
    name = "grand_prix"
    title = "▚ G R A N D   P R I X ▚"
    interval = 180.0                                   # refresh every 3 min
    CONFIG = {
        "org":           {"default": None},
        "repos":         {"default": []},
        "day_start":     {"default": "07:00"},
        "day_end":       {"default": "18:00"},
        "tz":            {"default": None},
        "sprint_anchor": {"default": "2026-06-15"},   # start date of Sprint 1
        "sprint_days":   {"default": 14},             # cadence between sprints
        "circuits":      {"default": None},           # override built-in F1 list
        "weights":       {"default": {}},
        "points":        {"default": [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]},
        "exclude":       {"default": []},
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
                   "--jq", '.[] | (.author.login // "unknown")'],
                  30)
        counts = {}
        for line in (out or "").splitlines():
            who = line.strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            counts[who] = counts.get(who, 0) + 1
        return counts

    def _pr_search(self, repo, qualifier, since_iso, until_iso, exclude):
        """logins -> PR count for a search qualifier (e.g. 'is:merged merged' or
        'created') over the window."""
        field, rng = qualifier
        q = f"repo:{repo} is:pr {field} {rng}:{since_iso}..{until_iso}"
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

    def _prs_merged(self, repo, since_iso, until_iso, exclude):
        return self._pr_search(repo, ("is:merged", "merged"),
                               since_iso, until_iso, exclude)

    def _prs_opened(self, repo, since_iso, until_iso, exclude):
        return self._pr_search(repo, ("", "created"),
                               since_iso, until_iso, exclude)

    _REVIEW_Q = ("query($owner:String!,$name:String!){repository(owner:$owner,"
                 "name:$name){pullRequests(first:50,orderBy:{field:UPDATED_AT,"
                 "direction:DESC}){nodes{reviews(first:50){nodes{"
                 "author{login} submittedAt state}}}}}}")

    # which review states count as "substantive" vs a drive-by comment
    _SUBSTANTIVE = {"APPROVED", "CHANGES_REQUESTED"}

    def _reviews(self, repo, start, until, exclude):
        """Return (substantive, comment) dicts of login -> count for PR reviews
        submitted in the window. Substantive = approve / changes-requested;
        comment = comment-only. Scans the 50 most-recently-updated PRs."""
        sub, com = {}, {}
        try:
            owner, name = repo.split("/", 1)
        except ValueError:
            return sub, com
        out = run(["gh", "api", "graphql", "-f", f"query={self._REVIEW_Q}",
                   "-F", f"owner={owner}", "-F", f"name={name}", "--jq",
                   ".data.repository.pullRequests.nodes[].reviews.nodes[] | "
                   '[(.author.login // ""), .submittedAt, .state] | @tsv'], 30)
        for line in (out or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            who, ts, state = parts[0].strip(), parts[1].strip(), parts[2].strip()
            if not who or who in exclude or who.endswith("[bot]"):
                continue
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (start <= when <= until):
                continue
            if state in self._SUBSTANTIVE:
                sub[who] = sub.get(who, 0) + 1
            elif state == "COMMENTED":
                com[who] = com.get(who, 0) + 1
        return sub, com

    def _load(self):
        try:
            with open(SEASON_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"races": {}}

    @staticmethod
    def _accumulate(orders, points):
        """Award finishing-position points across a list of finishing orders
        (each a list of logins, best first) -> a sorted standings table."""
        agg = {}
        for order in orders:
            for i, login in enumerate(order):
                a = agg.setdefault(login, {"login": login, "points": 0,
                                           "wins": 0, "podiums": 0})
                a["points"] += points[i] if i < len(points) else 0
                if i == 0:
                    a["wins"] += 1
                if i < 3:
                    a["podiums"] += 1
        return sorted(agg.values(), key=lambda r: -r["points"])

    # ---- sprint cadence (config-driven; drift counts its own Sprint 1, 2, …) --
    def _anchor(self, cfg):
        try:
            return date.fromisoformat(cfg.get("sprint_anchor") or "2026-06-15")
        except (ValueError, TypeError):
            return date(2026, 6, 15)

    def _sdays(self, cfg):
        try:
            return max(1, int(cfg.get("sprint_days") or 14))
        except (ValueError, TypeError):
            return 14

    def _circuits(self, cfg):
        c = cfg.get("circuits")
        if not (isinstance(c, list) and c):
            return CIRCUITS
        # accept a list of names or of [flag, name] pairs; normalize to pairs
        out = []
        for item in c:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append((item[0], item[1]))
            else:
                out.append(("", str(item)))
        return out

    @staticmethod
    def _sprint_idx(d, anchor, days):
        return (d - anchor).days // days            # 0 = the anchor sprint (#1)

    def _sprints(self, races, anchor, days):
        """Group stored daily race finishing-orders by sprint index."""
        groups = {}
        for dstr, race in races.items():
            try:
                idx = self._sprint_idx(date.fromisoformat(dstr), anchor, days)
            except ValueError:
                continue
            groups.setdefault(idx, []).append(race.get("order", []))
        return groups

    def _sprint_meta(self, idx, anchor, days, circuits):
        """(number, flag, circuit-name, window-string) for a sprint index."""
        flag, cname = circuits[idx % len(circuits)]
        s_start = anchor + timedelta(days=idx * days)
        s_end = s_start + timedelta(days=days - 1)
        return idx + 1, flag, cname, f"{_fmt_date(s_start)}–{_fmt_date(s_end)}"

    def _save(self, doc):
        # the season file holds colleague logins + rankings — keep it private
        # to this user (0700 dir / 0600 file) on shared machines.
        try:
            os.makedirs(os.path.dirname(SEASON_FILE), mode=0o700, exist_ok=True)
            tmp = SEASON_FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, indent=2)
            os.replace(tmp, SEASON_FILE)
            os.chmod(SEASON_FILE, 0o600)
        except OSError:
            pass

    def _bank(self, doc, key, order, fastest):
        doc.setdefault("races", {})[key] = {"order": order, "fastest": fastest}
        self._save(doc)

    def fetch(self, cfg):
        now, start, end = self._bounds(cfg)
        points = cfg.get("points") or [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
        exclude = set(cfg.get("exclude") or [])
        # grading rubric (manifest-configurable). Each action has a base weight
        # and a "repeat modifier": the k-th action of a type is worth
        # weight * modifier**(k-1), so extra actions in one category taper off —
        # rewarding breadth and resisting farming without a hard cap. A modifier
        # of 1.0 means linear (no taper). Merged PRs carry the highest base and
        # the gentlest taper, so shipping still scales.
        weights = cfg.get("weights") or {}
        w_merged = float(weights.get("pr_merged", 5))
        w_open = float(weights.get("pr_open", 3))
        w_review = float(weights.get("review", 2))         # approve / changes
        w_rcom = float(weights.get("review_comment", 1))   # comment-only
        w_commit = float(weights.get("commit", 1))
        m_merged = float(weights.get("pr_merged_mod", 0.85))
        m_open = float(weights.get("pr_open_mod", 0.6))
        m_review = float(weights.get("review_mod", 0.7))
        m_rcom = float(weights.get("review_comment_mod", 0.5))
        m_commit = float(weights.get("commit_mod", 0.4))

        def taper(count, weight, mod):
            if count <= 0:
                return 0.0
            if mod == 1.0:
                return weight * count
            return weight * (1 - mod ** count) / (1 - mod)

        state = "pre" if now < start else ("racing" if now < end else "done")

        drivers = []
        if state != "pre":
            until = min(now, end)
            sc, so, sm, rs, rc = {}, {}, {}, {}, {}
            for repo in cfg.get("repos") or []:
                si, ui = start.isoformat(), until.isoformat()
                for who, n in self._commits(repo, si, ui, exclude).items():
                    sc[who] = sc.get(who, 0) + n
                for who, n in self._prs_opened(repo, si, ui, exclude).items():
                    so[who] = so.get(who, 0) + n
                for who, n in self._prs_merged(repo, si, ui, exclude).items():
                    sm[who] = sm.get(who, 0) + n
                sub, com = self._reviews(repo, start, until, exclude)
                for who, n in sub.items():
                    rs[who] = rs.get(who, 0) + n
                for who, n in com.items():
                    rc[who] = rc.get(who, 0) + n
            for w in set(sc) | set(so) | set(sm) | set(rs) | set(rc):
                c, o, m = sc.get(w, 0), so.get(w, 0), sm.get(w, 0)
                rv, rcm = rs.get(w, 0), rc.get(w, 0)
                score = (taper(m, w_merged, m_merged)
                         + taper(o, w_open, m_open)
                         + taper(rv, w_review, m_review)
                         + taper(rcm, w_rcom, m_rcom)
                         + taper(c, w_commit, m_commit))
                drivers.append({"login": w, "commits": c, "prs_open": o,
                                "prs_merged": m, "reviews": rv,
                                "review_comments": rcm, "score": score})
            drivers.sort(key=lambda d: -d["score"])

        # bank today's daily race result once we're past the flag
        doc = self._load()
        races = doc.get("races", {})
        key = start.strftime("%Y-%m-%d")
        if state == "done" and key not in races and drivers:
            self._bank(doc, key, [d["login"] for d in drivers[:len(points)]],
                       drivers[0]["login"])
            races = doc.get("races", {})

        # sprint cadence: which GP are we in right now
        anchor, days = self._anchor(cfg), self._sdays(cfg)
        circuits = self._circuits(cfg)
        today = now.date()
        idx = self._sprint_idx(today, anchor, days)
        s_start = anchor + timedelta(days=idx * days)
        number, flag, cname, window = self._sprint_meta(idx, anchor, days, circuits)
        groups = self._sprints(races, anchor, days)

        # archive every COMPLETED sprint (its window fully past) exactly once, so
        # the championship is an immutable record — frozen GP results that never
        # recompute, not a live re-derivation from raw daily races.
        archive = doc.get("sprints", {})
        changed = False
        for g in sorted(groups):
            if g < idx and str(g) not in archive:        # finished & not yet stamped
                order = [r["login"] for r in self._accumulate(groups[g], points)]
                num, fl, cn, win = self._sprint_meta(g, anchor, days, circuits)
                archive[str(g)] = {"number": num, "circuit": cn, "flag": fl,
                                   "window": win, "order": order,
                                   "winner": order[0] if order else None,
                                   "stamped": today.isoformat()}
                changed = True
        if changed:
            doc["sprints"] = archive
            self._save(doc)

        # current sprint board: live from this sprint's daily races (in progress,
        # NOT yet scored into the championship)
        sprint_board = self._accumulate(groups.get(idx, []), points)
        # season championship: only COMPLETED sprints, from their frozen results
        season_orders = [archive[k]["order"] for k in sorted(archive, key=int)]
        season_board = self._accumulate(season_orders, points)

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
            "gp_name": f"{cname.upper()} GP",
            "gp_flag": flag,
            "sprint_number": number,
            "sprint_window": window,
            "race_no": (today - s_start).days + 1,
            "race_total": days,
            "races_done": len(groups.get(idx, [])),
            "sprint_board": sprint_board,
            "season_board": season_board,
            "season_sprints": len(archive),
        }}

    # ---- rendering --------------------------------------------------------
    def build(self):
        self.t = 0.0
        self.disp = {}            # login -> displayed track fraction (smoothed)
        self.view_t = 0.0
        self.view_i = 0           # index into the available views
        self.view = "race"
        self.prev_order = []
        self.flash = {}           # login -> overtake flash timer
        self.boost = {}           # login -> "pulling ahead" timer (drives exhaust)

    def update(self, dt, frame, st):
        self.t += dt
        self.view_t += dt
        gp = st.get("grand_prix") or {}
        # rotate RACE -> SPRINT -> SEASON, skipping boards that have no data yet
        avail = ["race"]
        if gp.get("sprint_board"):
            avail.append("sprint")
        if gp.get("season_board"):
            avail.append("season")
        if self.view_t > 14.0:
            self.view_t = 0.0
            self.view_i += 1
        self.view = avail[self.view_i % len(avail)]
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
        # ease each car toward its score-based target fraction; a car whose
        # position is advancing is "pulling ahead" -> earns an exhaust trail
        for d in drivers:
            login = d["login"]
            target = (d["score"] / lead) if lead > 0 else 0.0
            cur = self.disp.get(login, 0.0)
            nxt = cur + (target - cur) * min(1.0, dt * 1.5)
            if nxt > cur + 0.0008:                    # gaining track position
                self.boost[login] = 0.8
            self.disp[login] = nxt
        for k in list(self.boost):
            self.boost[k] = max(0.0, self.boost[k] - dt)

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
        if self.view == "sprint":
            self._draw_sprint(scr, gp)
        elif self.view == "season":
            self._draw_season(scr, gp)
        else:
            self._draw_race(scr, frame, gp)

    def _draw_race(self, scr, frame, gp):
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
        put(scr, 5, 3, f"{org}  ·  {len(gp.get('drivers') or [])} on track",
            cp_(C_WHITE) | curses.A_DIM)

        drivers = gp.get("drivers") or []
        if not drivers:
            msg = ("warming up — lights out at " + gp["day_start"]
                   if gp["state"] == "pre" else "no commits logged yet today")
            center(scr, self.h // 2, msg, cp_(C_YELLOW) | curses.A_DIM)
            return

        top = 6
        lane_h = 2
        # reserve the space below the lanes for the scorecard when the terminal
        # is tall enough (scorecard ≈ 3 header rows + 1 row per driver).
        fit = (self.h - top - 5) // (lane_h + 1)
        show_card = fit >= 2
        if show_card:
            n = min(len(drivers), 10, max(1, fit))
        else:
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
            xcar = int(lerp(track_l, track_r - CAR_LEN, frac))
            overtaking = self.flash.get(d["login"], 0.0) > 0
            pulling = overtaking or self.boost.get(d["login"], 0.0) > 0
            # exhaust trails BEHIND the car (it travels right, so puffs go left)
            if pulling:
                for j, ch in enumerate(("≈", "≈", "~", "·"), start=1):
                    ex = xcar - j
                    if ex <= track_l:
                        break
                    a = curses.A_BOLD if j <= 2 else curses.A_DIM
                    putch(scr, y, ex, ch, cp_(C_YELLOW) | a)
            carcol = C_GREEN if pulling else pcol
            put(scr, y, xcar, car_sprite(frame, pulling), cp_bold(carcol))
            # label + gap to leader
            gap = lead - d["score"]
            tag = (f"{d['login'][:16]}  {int(d['score'])}"
                   + ("" if i == 0 else f"  +{int(gap)}"))
            put(scr, y, track_r + 2, tag,
                cp_bold(pcol) if i < 3 else cp_(C_WHITE))
            if overtaking:
                put(scr, y, xcar - 9, "⚡OVERTAKE", cp_bold(C_YELLOW))

        if show_card:
            self._draw_scorecard(scr, top + n * lane_h + 1, drivers, n)

        lead_name = drivers[0]["login"]
        put(scr, self.h - 2, 3, f"P1 {lead_name} leads the Grand Prix",
            cp_bold(C_YELLOW))

    # column x-offsets for the scorecard table
    _SC_COLS = (("POS", 3), ("DRIVER", 7), ("MRG", 26), ("OPN", 32),
                ("REV", 38), ("COM", 44), ("CMT", 50), ("PTS", 57))

    def _draw_scorecard(self, scr, y0, drivers, n):
        """Below the lanes: the grading rubric + a per-driver breakdown of how
        each score is composed (merged / opened / reviews / comments / commits)."""
        if y0 >= self.h - 2:
            return
        put(scr, y0, 3, "── SCORECARD " + "─" * 10, cp_(C_BLUE) | curses.A_DIM)
        put(scr, y0 + 1, 3,
            "grading:  PR merged ×5   opened ×3   review ×2   comment ×1   "
            "commit ×1   · repeats taper off", cp_(C_CYAN) | curses.A_DIM)
        hy = y0 + 2
        for label, x in self._SC_COLS:
            put(scr, hy, x, label, cp_(C_WHITE) | curses.A_DIM)
        for i in range(n):
            ry = hy + 1 + i
            if ry >= self.h - 2:
                break
            d = drivers[i]
            pcol = POS_COLORS.get(i + 1, C_WHITE)
            put(scr, ry, 3, f"P{i + 1}", cp_bold(pcol))
            put(scr, ry, 7, d["login"][:17], cp_(pcol if i < 3 else C_WHITE))
            for x, v in ((26, d["prs_merged"]), (32, d["prs_open"]),
                         (38, d["reviews"]), (44, d["review_comments"]),
                         (50, d["commits"])):
                put(scr, ry, x, f"{v:>3}", cp_(C_WHITE))
            put(scr, ry, 57, f"{int(d['score']):>4}",
                cp_bold(pcol if i < 3 else C_WHITE))

    def _draw_table(self, scr, title, subtitle, table, win_glyph=None):
        """Shared standings table (used by SPRINT and SEASON views): a ranked
        list with a points bar and an optional ×N trophy/win count."""
        center(scr, 3, title, cp_bold(C_CYAN))
        if subtitle:
            center(scr, 4, subtitle, cp_(C_WHITE) | curses.A_DIM)
        if not table:
            center(scr, self.h // 2, "no results banked yet — race on",
                   cp_(C_YELLOW) | curses.A_DIM)
            return
        top = 6
        maxp = table[0]["points"] or 1
        n = min(len(table), self.h - top - 2, 12)
        for i in range(n):
            r = table[i]
            y = top + i
            pcol = POS_COLORS.get(i + 1, C_WHITE)
            barw = int((r["points"] / maxp) * max(6, self.w // 3))
            put(scr, y, 3, f"{i + 1:>2}  {r['login'][:18]:<18}", cp_bold(pcol))
            put(scr, y, 26, "█" * barw, cp_(pcol))
            extra = f"{r['points']} pts"
            if win_glyph and r.get("wins"):
                extra += f"  {r['wins']}×{win_glyph}"
            put(scr, y, 26 + barw + 1, extra, cp_(C_WHITE) | curses.A_DIM)

    def _draw_sprint(self, scr, gp):
        title = f"{gp.get('gp_flag', '')}  {gp.get('gp_name', 'GRAND PRIX')}".strip()
        sub = (f"Sprint {gp.get('sprint_number', '?')}  ·  {gp.get('sprint_window', '')}"
               f"  ·  race {gp.get('race_no', '?')} of {gp.get('race_total', '?')}")
        # daily race wins this sprint get a chequered flag
        self._draw_table(scr, title, sub, gp.get("sprint_board") or [], "🏁")

    def _draw_season(self, scr, gp):
        n = gp.get("season_sprints", 0)
        sub = f"after {n} Grand Prix" if n == 1 else f"after {n} Grands Prix"
        # season "wins" = sprints (GPs) won, marked with a trophy
        self._draw_table(scr, "DRIVERS' CHAMPIONSHIP", sub,
                         gp.get("season_board") or [], "\U0001F3C6")

    def hud(self, st):
        gp = st.get("grand_prix") or {}
        sb = gp.get("sprint_board") or []
        top = sb[0] if sb else None
        return [("grand prix", f"{gp.get('gp_flag','')} {gp.get('gp_name','—')}".strip()),
                ("sprint", f"#{gp.get('sprint_number','?')}  {gp.get('sprint_window','')}"),
                ("race", f"{gp.get('race_no','?')} of {gp.get('race_total','?')}"
                         f"  ({gp.get('state','—')})"),
                ("on track", str(len(gp.get("drivers") or []))),
                ("sprint P1", f"{top['login']} {top['points']}pts" if top else "—")]
