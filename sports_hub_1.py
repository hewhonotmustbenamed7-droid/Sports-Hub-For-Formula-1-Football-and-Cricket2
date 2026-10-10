# sports_hub.py - F1 / Football / Cricket dashboard with history + diagnostics
# Standard library only. Python 3.8+
import json, re, time, queue, threading, traceback
import urllib.request, urllib.parse, urllib.error
from collections import deque
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timedelta, timezone, date
from concurrent.futures import ThreadPoolExecutor

NL = chr(10)          # newline constant (avoids backslash-n inside strings)

# ---------------------------------------------------------------- settings
CRICAPI_KEY = "d5efd27e-ca08-4484-90ab-4356ea572e6a"

JOLPICA = "https://api.jolpi.ca/ergast/f1"
OPENF1 = "https://api.openf1.org/v1"
ESPN = "https://site.api.espn.com/apis"
CRICAPI = "https://api.cricapi.com/v1"

# name: (ESPN code, calendar-year tournament?)
COMPETITIONS = {
    "Premier League": ("eng.1", False),
    "La Liga": ("esp.1", False),
    "Serie A": ("ita.1", False),
    "Bundesliga": ("ger.1", False),
    "Ligue 1": ("fra.1", False),
    "Champions League": ("uefa.champions", False),
    "Europa League": ("uefa.europa", False),
    "Conference League": ("uefa.europa.conf", False),
    "FA Cup": ("eng.fa", False),
    "League Cup (Carabao)": ("eng.league_cup", False),
    "Copa del Rey": ("esp.copa_del_rey", False),
    "DFB Pokal": ("ger.dfb_pokal", False),
    "UEFA Nations League": ("uefa.nations", False),
    "FIFA World Cup": ("fifa.world", True),
    "UEFA Euro": ("uefa.euro", True),
    "Copa America": ("conmebol.america", True),
    "Copa Libertadores": ("conmebol.libertadores", True),
    "Africa Cup of Nations": ("caf.nations", True),
    "Gold Cup": ("concacaf.gold", True),
    "MLS": ("usa.1", True),
}
AUTO_REFRESH_SECONDS = 60

# ---------------------------------------------------------------- logging
LOG = deque(maxlen=500)
LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        LOG.append(time.strftime("%H:%M:%S") + "  " + msg)


def mask(url):
    return re.sub(r"(apikey=)[^&]+", r"\1***", url)


# ---------------------------------------------------------------- networking
class FetchError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def describe(e):
    """Turn a network exception into a readable one-line message."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")[:120].replace(NL, " ")
        except Exception:
            body = ""
        return ("HTTP " + str(e.code) + " " + str(e.reason) + " " + body).strip()
    if isinstance(e, urllib.error.URLError):
        r = str(e.reason)
        low = r.lower()
        hint = ""
        if "certificate" in low:
            hint = " -> SSL certificate problem (macOS: run Install Certificates.command in the Python folder)"
        elif "getaddrinfo" in low or "name or service" in low or "resolution" in low:
            hint = " -> no internet connection or DNS problem"
        elif "timed out" in low:
            hint = " -> connection timed out"
        return "Network error: " + r + hint
    if "timed out" in str(e).lower():
        return "Request timed out"
    return type(e).__name__ + ": " + str(e)


_cache = {}


def get_json(url, ttl=30):
    """Download JSON with caching, one retry, and readable errors."""
    url = urllib.parse.quote(url, safe=":/?&=<>+%")
    now = time.time()
    hit = _cache.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    last = None
    for attempt in (1, 2):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SportsHub/2.1",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except ValueError:
                raise FetchError("Server did not return JSON: " + raw[:80].replace(NL, " "))
            _cache[url] = (now, data)
            ms = int((time.time() - t0) * 1000)
            log("OK   " + str(ms).rjust(5) + " ms  " + mask(url))
            return data
        except FetchError as e:
            last = e
            break
        except urllib.error.HTTPError as e:
            last = FetchError(describe(e), e.code)
            if e.code < 500:
                break
        except Exception as e:
            last = FetchError(describe(e))
        time.sleep(1)
    log("FAIL " + mask(url) + NL + "         -> " + str(last))
    raise last


def parse_dt(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- base tab
class BaseTab(ttk.Frame):
    LEFT_TITLE = ""
    RIGHT_TITLE = ""

    def __init__(self, parent):
        super().__init__(parent)
        self.q = queue.Queue()
        self.job = 0
        self.loaded = False

        self.top = ttk.Frame(self)
        self.top.pack(fill="x", padx=8, pady=(6, 2))
        self.top2 = ttk.Frame(self)
        self.top2.pack(fill="x", padx=8, pady=(0, 2))

        row2 = ttk.Frame(self)
        row2.pack(fill="x", padx=8)
        self.banner = ttk.Label(row2, text="", font=("Arial", 9, "bold"))
        self.banner.pack(side="left")
        self.status = ttk.Label(row2, text="")
        self.status.pack(side="right")

        tf = ttk.Frame(self)
        tf.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(tf, show="headings", height=11)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=6)
        self.left = self._textbox(box, self.LEFT_TITLE)
        self.right = self._textbox(box, self.RIGHT_TITLE)
        self._poll()

    def _textbox(self, parent, title):
        f = ttk.LabelFrame(parent, text=title)
        f.pack(side="left", fill="both", expand=True, padx=3)
        t = tk.Text(f, height=10, width=50, font=("Consolas", 9), state="disabled", wrap="none")
        sy = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        sx = ttk.Scrollbar(f, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        t.pack(fill="both", expand=True)
        return t

    def set_text(self, box, text):
        box.config(state="normal")
        box.delete("1.0", "end")
        box.insert("end", text)
        box.config(state="disabled")

    def show_table(self, cols, rows, widths=None):
        widths = widths or {}
        self.tree["columns"] = cols
        for c in cols:
            self.tree.heading(c, text=c)
            if c in widths:
                self.tree.column(c, width=widths[c], anchor="w")
            else:
                self.tree.column(c, width=60, anchor="center")
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(i), values=row)

    def run(self, work, done, track=True):
        """Run work() in a thread, then call done(result) on the main thread."""
        if track:
            self.job += 1
            tok = self.job
            self.status.config(text="Loading...")
        else:
            tok = None

        def target():
            try:
                self.q.put((tok, done, work(), None))
            except FetchError as e:
                self.q.put((tok, None, None, str(e)))
            except Exception as e:
                log("BUG  " + traceback.format_exc())
                self.q.put((tok, None, None, type(e).__name__ + ": " + str(e)))
        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            while True:
                tok, done, res, err = self.q.get_nowait()
                if tok is not None and tok != self.job:
                    continue                      # an older request, ignore it
                if err:
                    if tok is not None:
                        self.status.config(text="Error")
                        self.set_text(self.left, "Error:" + NL + err + NL + NL +
                                      "Open the Diagnostics tab and press Run self-test" + NL +
                                      "to see which data source is failing.")
                    continue
                if tok is not None:
                    self.status.config(text="Updated " + time.strftime("%H:%M:%S"))
                try:
                    done(res)
                except Exception as e:
                    log("BUG  display error:" + NL + traceback.format_exc())
                    self.status.config(text="Display error")
                    self.set_text(self.left, "Display error:" + NL + type(e).__name__ + ": " + str(e) +
                                  NL + NL + "Details are in the Diagnostics tab.")
        except queue.Empty:
            pass
        self.after(200, self._poll)

    def ensure_loaded(self):
        if not self.loaded:
            self.loaded = True
            self.refresh()

    def refresh(self):
        pass


# ---------------------------------------------------------------- F1
class F1Tab(BaseTab):
    LEFT_TITLE = "Race result"
    RIGHT_TITLE = "Race info / Qualifying"

    def __init__(self, parent, root):
        super().__init__(parent)
        self.root = root
        self.races = []
        this_year = utcnow().year
        self.season = tk.StringVar(value=str(this_year))
        self.race = tk.StringVar()
        self.view = tk.StringVar(value="drivers")

        ttk.Label(self.top, text="Season").pack(side="left")
        sc = ttk.Combobox(self.top, textvariable=self.season, state="readonly", width=6,
                          values=[str(y) for y in range(this_year, 1949, -1)])
        sc.pack(side="left", padx=4)
        sc.bind("<<ComboboxSelected>>", lambda e: self.load_season())

        ttk.Label(self.top, text="Race").pack(side="left", padx=(8, 0))
        self.race_box = ttk.Combobox(self.top, textvariable=self.race, state="readonly", width=38)
        self.race_box.pack(side="left", padx=4)
        self.race_box.bind("<<ComboboxSelected>>", lambda e: self.load_race())

        ttk.Radiobutton(self.top, text="Drivers", variable=self.view, value="drivers",
                        command=self.load_race).pack(side="left", padx=2)
        ttk.Radiobutton(self.top, text="Constructors", variable=self.view, value="constructors",
                        command=self.load_race).pack(side="left", padx=2)
        ttk.Button(self.top, text="Refresh", command=self.refresh).pack(side="left", padx=6)
        ttk.Button(self.top, text="Race Map", command=lambda: RaceMap(self.root)).pack(side="left")

    def refresh(self):
        if not self.races:
            self.load_season()
        else:
            self.load_race()
        self.load_banner()

    def load_banner(self):
        def work():
            parts = []
            try:
                s = get_json(OPENF1 + "/sessions?session_key=latest", ttl=20)[-1]
                start, end = parse_dt(s["date_start"]), parse_dt(s["date_end"])
                name = s["session_name"] + " - " + s["circuit_short_name"]
                if start <= utcnow() <= end:
                    parts.append("LIVE NOW: " + name)
                else:
                    parts.append("No session running (latest: " + name + ", ended " +
                                 end.strftime("%d %b %H:%M") + " UTC)")
            except Exception:
                parts.append("Live session info unavailable")
            try:
                nxt = get_json(JOLPICA + "/current/next.json", ttl=300)["MRData"]["RaceTable"]["Races"]
                if nxt:
                    n = nxt[0]
                    parts.append("Next race: " + n["raceName"] + " on " + n["date"])
            except Exception:
                pass
            return "   |   ".join(parts)
        self.run(work, lambda t: self.banner.config(text=t), track=False)

    def load_season(self):
        yr = self.season.get()

        def work():
            url = JOLPICA + "/" + yr + "/races.json?limit=100"
            return get_json(url, ttl=3600)["MRData"]["RaceTable"]["Races"]
        self.run(work, self._season_loaded)

    def _season_loaded(self, races):
        self.races = races
        self.race_box["values"] = ["R" + r["round"] + " - " + r["raceName"] + " (" + r["date"] + ")"
                                   for r in races]
        if not races:
            self.race.set("")
            self.show_table(("Info",), [("No races found for this season",)], {"Info": 400})
            self.set_text(self.left, "")
            self.set_text(self.right, "")
            return
        today = utcnow().date()
        past = [i for i, r in enumerate(races) if date.fromisoformat(r["date"]) <= today]
        self.race_box.current(past[-1] if past else 0)   # latest race already run
        self.load_race()

    def load_race(self):
        i = self.race_box.current()
        if i < 0:
            return
        r = self.races[i]
        yr, rnd, view = r["season"], r["round"], self.view.get()

        def work():
            base = JOLPICA + "/" + yr + "/" + rnd
            res = get_json(base + "/results.json?limit=100", ttl=600)["MRData"]["RaceTable"]["Races"]
            try:
                q = get_json(base + "/qualifying.json?limit=100", ttl=600)["MRData"]["RaceTable"]["Races"]
            except Exception:
                q = []
            kind = "constructorStandings" if view == "constructors" else "driverStandings"
            try:
                st = get_json(base + "/" + kind + ".json?limit=100",
                              ttl=600)["MRData"]["StandingsTable"]["StandingsLists"]
            except Exception:
                st = []
            return r, res, q, st, view
        self.run(work, self._race_loaded)

    def _race_loaded(self, data):
        r, res, q, st, view = data

        rows = []
        cols = ("Info",)
        if st and view == "constructors":
            cols = ("Pos", "Constructor", "Pts", "Wins")
            for s in st[0].get("ConstructorStandings", []):
                rows.append((s["positionText"], s["Constructor"]["name"], s["points"], s["wins"]))
        elif st:
            cols = ("Pos", "Driver", "Team", "Pts", "Wins")
            for s in st[0].get("DriverStandings", []):
                d = s["Driver"]
                team = s["Constructors"][0]["name"] if s.get("Constructors") else ""
                rows.append((s["positionText"], d["givenName"] + " " + d["familyName"],
                             team, s["points"], s["wins"]))
        if not rows:
            cols, rows = ("Info",), [("No standings available for this selection",)]
        self.show_table(cols, rows, {"Driver": 200, "Team": 180, "Constructor": 220, "Info": 400})

        if res:
            race = res[0]
            lines = [race["raceName"] + " " + race["season"] + "  (" + race["date"] + ")", ""]
            for x in race["Results"]:
                d = x["Driver"]
                t = x.get("Time", {}).get("time", x["status"])
                lines.append(x["positionText"].rjust(3) + "  " +
                             (d["givenName"][0] + ". " + d["familyName"]).ljust(18) +
                             x["Constructor"]["name"].ljust(16) + " " +
                             str(t).ljust(14) + " +" + str(x["points"]).ljust(3) +
                             " grid " + str(x["grid"]))
            self.set_text(self.left, NL.join(lines))
        else:
            self.set_text(self.left, "No results available (race not run yet).")

        c = r["Circuit"]
        loc = c["Location"]
        info = ["Round " + r["round"] + ": " + r["raceName"],
                "Circuit: " + c["circuitName"],
                "Location: " + loc["locality"] + ", " + loc["country"],
                "Date: " + r["date"] + " " + r.get("time", ""),
                "Standings above are after round " + r["round"] + ".", ""]
        if q and q[0].get("QualifyingResults"):
            info.append("Qualifying:")
            for x in q[0]["QualifyingResults"]:
                d = x["Driver"]
                info.append(str(x["position"]).rjust(3) + "  " +
                            (d["givenName"][0] + ". " + d["familyName"]).ljust(18) +
                            x.get("Q1", "").ljust(10) + " " + x.get("Q2", "").ljust(10) +
                            " " + x.get("Q3", ""))
        else:
            info.append("No qualifying data for this race.")
        self.set_text(self.right, NL.join(info))


# ---------------------------------------------------------------- Football
# NOTE: "leg" is deliberately NOT here because "league" contains "leg".
STAGE_WORDS = ("round", "final", "group", "phase", "playoff", "knockout",
               "quarter", "semi", "qualif", "prelim")


def clean_stage(s):
    """Merge two-legged ties: Quarterfinals - 2nd Leg -> Quarterfinals."""
    return re.sub(r"\s*[-,:]?\s*(1st|2nd|first|second)\s*leg.*$", "", s, flags=re.I).strip()


def pretty_stage(slug):
    return clean_stage(slug.replace("-", " ").replace("_", " ").strip().title())


def stage_of(ev, when):
    comp = ev["competitions"][0]
    for src in (comp.get("notes"), ev.get("notes")):
        for n in src or []:
            h = n.get("headline") or n.get("text")
            if h:
                return clean_stage(h)
    slug = ((ev.get("season") or {}).get("slug") or "").lower()
    if slug and any(w in slug for w in STAGE_WORDS):
        return pretty_stage(slug)
    return when.strftime("%b %Y")            # fallback (normal for leagues): group by month


def parse_match(ev):
    comp = ev["competitions"][0]
    st = ev["status"]["type"]
    when = parse_dt(ev["date"])
    cs = sorted(comp["competitors"], key=lambda c: c.get("homeAway") != "home")
    names, scores = [], []
    for c in cs:
        names.append(c.get("team", {}).get("displayName") or c.get("athlete", {}).get("displayName", "?"))
        sc = c.get("score", "")
        if isinstance(sc, dict):
            sc = sc.get("displayValue", "")
        scores.append(str(sc))
    while len(names) < 2:
        names.append("?")
        scores.append("")
    if st["state"] == "pre":
        line = when.strftime("%d %b %H:%M") + "  " + names[0] + " vs " + names[1]
    else:
        detail = st.get("shortDetail") or st.get("description", "")
        line = (when.strftime("%d %b") + "  " + names[0] + " " + scores[0] + " - " +
                scores[1] + " " + names[1] + "   [" + detail + "]")
    return {"stage": stage_of(ev, when), "when": when, "state": st["state"], "line": line}


class FootballTab(BaseTab):
    LEFT_TITLE = "Matches in selected stage"
    RIGHT_TITLE = "Live now / Next matches"
    ALL = "All stages"

    def __init__(self, parent):
        super().__init__(parent)
        self.matches = []
        self.comp = tk.StringVar(value="Champions League")
        self.season = tk.StringVar()
        self.stage = tk.StringVar(value=self.ALL)

        ttk.Label(self.top, text="Competition").pack(side="left")
        cc = ttk.Combobox(self.top, textvariable=self.comp, values=list(COMPETITIONS),
                          state="readonly", width=22)
        cc.pack(side="left", padx=4)
        cc.bind("<<ComboboxSelected>>", lambda e: self.comp_changed())

        ttk.Label(self.top, text="Season").pack(side="left", padx=(8, 0))
        self.season_box = ttk.Combobox(self.top, textvariable=self.season, state="readonly", width=6)
        self.season_box.pack(side="left", padx=4)
        self.season_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        ttk.Label(self.top, text="Stage").pack(side="left", padx=(8, 0))
        self.stage_box = ttk.Combobox(self.top, textvariable=self.stage, state="readonly", width=26)
        self.stage_box.pack(side="left", padx=4)
        self.stage_box.bind("<<ComboboxSelected>>", lambda e: self.show_stage())

        ttk.Button(self.top, text="Refresh", command=self.refresh).pack(side="left", padx=6)
        self.set_seasons()

    def set_seasons(self):
        now = utcnow()
        calendar = COMPETITIONS[self.comp.get()][1]
        if calendar:
            years = [str(y) for y in range(now.year, 1989, -1)]
        else:
            first = now.year if now.month >= 8 else now.year - 1
            years = [str(y) for y in range(first, 1999, -1)]
        self.season_box["values"] = years
        self.season.set(years[0])

    def comp_changed(self):
        self.set_seasons()
        self.refresh()

    def refresh(self):
        code, calendar = COMPETITIONS[self.comp.get()]
        y = int(self.season.get())
        if calendar:
            start, end = date(y, 1, 1), date(y, 12, 31)
        else:
            start, end = date(y, 7, 1), date(y + 1, 6, 30)
        today = utcnow().date()

        chunks, d0 = [], start
        while d0 <= end:
            e0 = min(d0 + timedelta(days=30), end)
            chunks.append((d0, e0))
            d0 = e0 + timedelta(days=1)

        def ttl_for(end_d):
            # recent/future ranges refresh often; old ranges are cached for hours
            return 60 if end_d >= today - timedelta(days=2) else 6 * 3600

        def work():
            rows, cols, notes = [], (), []

            # ---- league table (not available for pure knockout cups)
            try:
                url = ESPN + "/v2/sports/soccer/" + code + "/standings?season=" + str(y)
                data = get_json(url, ttl=600)
                children = data.get("children") or ([data] if "standings" in data else [])
                multi = len(children) > 1
                if multi:
                    cols = ("Group", "Pos", "Team", "P", "W", "D", "L", "GD", "Pts")
                else:
                    cols = ("Pos", "Team", "P", "W", "D", "L", "GD", "Pts")
                for group in children:
                    for i, en in enumerate(group["standings"]["entries"], 1):
                        st = {s["name"]: s.get("displayValue", s.get("value")) for s in en["stats"]}
                        row = (st.get("rank", i), en["team"]["displayName"], st.get("gamesPlayed", ""),
                               st.get("wins", ""), st.get("ties", ""), st.get("losses", ""),
                               st.get("pointDifferential", ""), st.get("points", ""))
                        rows.append(((group.get("name", ""),) + row) if multi else row)
            except FetchError as ex:
                notes.append("League table unavailable: " + str(ex))
            except Exception:
                log("BUG  standings parse:" + NL + traceback.format_exc())
                notes.append("League table could not be read (see Diagnostics tab)")

            # ---- matches: ranges first, split into smaller ranges if ESPN rejects one
            errors, budget = [], {"n": 0}

            def fetch_range(a, b, ttl):
                if budget["n"] > 150:
                    return []
                budget["n"] += 1
                base = ESPN + "/site/v2/sports/soccer/" + code + "/scoreboard?dates="
                if a == b:
                    url = base + a.strftime("%Y%m%d")
                else:
                    url = base + a.strftime("%Y%m%d") + "-" + b.strftime("%Y%m%d") + "&limit=300"
                try:
                    return get_json(url, ttl=ttl).get("events", [])
                except FetchError as ex:
                    if ex.code == 404:
                        raise
                    if a >= b:
                        errors.append(str(ex))
                        return []
                    mid = a + (b - a) // 2
                    return fetch_range(a, mid, ttl) + fetch_range(mid + timedelta(days=1), b, ttl)

            def fetch_chunk(c):
                return fetch_range(c[0], c[1], ttl_for(c[1]))

            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    results = list(pool.map(fetch_chunk, chunks))
            except FetchError as ex:
                if ex.code == 404:
                    raise FetchError("ESPN has no competition " + code + " (HTTP 404). "
                                     "Pick a different competition.")
                raise

            seen, matches = set(), []
            for events in results:
                for ev in events:
                    key = ev.get("id")
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        matches.append(parse_match(ev))
                    except Exception:
                        log("BUG  could not parse one match:" + NL + traceback.format_exc())
            if not matches and errors:
                raise FetchError("ESPN request failed for this competition/season:" + NL + errors[0])
            matches.sort(key=lambda m: m["when"])
            if errors:
                notes.append(str(len(errors)) + " date(s) failed to load. First error: " + errors[0])
            warn = (NL + NL + NL.join(notes)) if notes else ""
            return cols, rows, matches, warn

        self.run(work, self._loaded)

    def _loaded(self, data):
        cols, rows, self.matches, warn = data
        if rows:
            self.show_table(cols, rows, {"Team": 240, "Group": 120})
        else:
            self.show_table(("Info",), [("No league table for this competition/season "
                                         "(knockout cups have none) - use the Stage box",)],
                            {"Info": 600})

        order, first_seen = [], {}
        for m in self.matches:
            if m["stage"] not in first_seen:
                first_seen[m["stage"]] = m["when"]
                order.append(m["stage"])
        self.stage_box["values"] = [self.ALL] + order

        started = [m["stage"] for m in self.matches if m["state"] in ("in", "post")]
        self.stage.set(started[-1] if started else (order[0] if order else self.ALL))
        self.show_stage()

        live = [m["line"] for m in self.matches if m["state"] == "in"]
        now = utcnow()
        nxt = [m for m in self.matches if m["state"] == "pre" and m["when"] >= now][:10]
        parts = []
        if live:
            parts += ["LIVE NOW:"] + live + [""]
        if nxt:
            parts += ["NEXT MATCHES (UTC):"] + ["[" + m["stage"] + "]  " + m["line"] for m in nxt]
        body = NL.join(parts) if parts else "Nothing live or scheduled in this season."
        self.set_text(self.right, body + warn)

    def show_stage(self):
        pick = self.stage.get()
        sel = [m for m in self.matches if pick == self.ALL or m["stage"] == pick]
        if not sel:
            self.set_text(self.left, "No matches found. Try another season or stage.")
            return
        lines = [pick + "  (" + str(len(sel)) + " matches)", ""]
        last_stage = None
        for m in sel:
            if pick == self.ALL and m["stage"] != last_stage:
                if last_stage:
                    lines.append("")
                lines.append("== " + m["stage"] + " ==")
                last_stage = m["stage"]
            lines.append(m["line"])
        self.set_text(self.left, NL.join(lines))


# ---------------------------------------------------------------- Cricket (CricAPI)
TOURNAMENTS = {
    "Current matches": None,
    "IPL": "ipl",
    "T20 World Cup": "t20 world cup",
    "ODI World Cup": "world cup",
    "Asia Cup": "asia cup",
    "Champions Trophy": "champions trophy",
    "Big Bash (BBL)": "big bash",
    "Pakistan Super League": "psl",
    "The Hundred": "hundred",
    "Test series": "test",
    "Women's cricket": "women",
}

API_USAGE = {"text": ""}


def api(endpoint, params="", ttl=300):
    if CRICAPI_KEY.startswith("paste"):
        raise FetchError("Add your CricAPI key at the top of the file (CRICAPI_KEY).")
    d = get_json(CRICAPI + "/" + endpoint + "?apikey=" + CRICAPI_KEY + "&offset=0" + params, ttl=ttl)
    info = d.get("info") or {}
    if "hitsToday" in info:
        API_USAGE["text"] = ("CricAPI calls today: " + str(info.get("hitsToday")) + "/" +
                             str(info.get("hitsLimit", "?")))
    if d.get("status") != "success":
        raise FetchError("CricAPI said: " + str(d.get("status")) + " - " +
                         str(d.get("reason", d.get("message", ""))))
    return d


def match_text(m):
    """Short, tidy summary of one match."""
    title = m.get("name", "?")
    kind = str(m.get("matchType", "")).upper()
    lines = [title + (("   [" + kind + "]") if kind else "")]
    when = str(m.get("dateTimeGMT") or m.get("date") or "")[:16].replace("T", " ")
    if when:
        lines.append("  When:   " + when + " GMT")
    if m.get("venue"):
        lines.append("  Venue:  " + str(m["venue"]))
    if m.get("tossWinner"):
        lines.append("  Toss:   " + str(m["tossWinner"]) + " chose to " + str(m.get("tossChoice", "")))
    for s in m.get("score") or []:
        inn = str(s.get("inning", "")).replace(" Inning", "")
        lines.append("  " + inn.ljust(28) + " " + str(s.get("r", "?")) + "/" + str(s.get("w", "?")) +
                     "  (" + str(s.get("o", "?")) + " ov)")
    lines.append("  Status: " + str(m.get("status", "")))
    return NL.join(lines)


def scorecard_text(d):
    out = [match_text(d), ""]
    for inn in d.get("scorecard") or []:
        out.append("=" * 60)
        out.append(str(inn.get("inning", "")))
        out.append("Batter".ljust(22) + "How out".ljust(26) + "R".rjust(4) + "B".rjust(5) +
                   "4s".rjust(4) + "6s".rjust(4) + "SR".rjust(8))
        for b in inn.get("batting") or []:
            name = (b.get("batsman") or {}).get("name", "?")
            how = str(b.get("dismissal-text") or b.get("dismissal") or "")[:24]
            out.append(name[:21].ljust(22) + how.ljust(26) + str(b.get("r", "")).rjust(4) +
                       str(b.get("b", "")).rjust(5) + str(b.get("4s", "")).rjust(4) +
                       str(b.get("6s", "")).rjust(4) + str(b.get("sr", "")).rjust(8))
        out.append("")
        out.append("Bowler".ljust(22) + "O".rjust(6) + "M".rjust(4) + "R".rjust(5) +
                   "W".rjust(4) + "Econ".rjust(8))
        for b in inn.get("bowling") or []:
            name = (b.get("bowler") or {}).get("name", "?")
            out.append(name[:21].ljust(22) + str(b.get("o", "")).rjust(6) + str(b.get("m", "")).rjust(4) +
                       str(b.get("r", "")).rjust(5) + str(b.get("w", "")).rjust(4) +
                       str(b.get("eco", "")).rjust(8))
        out.append("")
    return NL.join(out)


class CricketTab(BaseTab):
    LEFT_TITLE = "Match detail / scorecard"
    RIGHT_TITLE = "Current matches"

    def __init__(self, parent):
        super().__init__(parent)
        self.preset = tk.StringVar(value="Current matches")
        self.search = tk.StringVar()
        self.series_name = tk.StringVar()
        self.series = []
        self.rows = []
        self.series_mode = False

        ttk.Label(self.top, text="Tournament").pack(side="left")
        pc = ttk.Combobox(self.top, textvariable=self.preset, values=list(TOURNAMENTS),
                          state="readonly", width=22)
        pc.pack(side="left", padx=4)
        pc.bind("<<ComboboxSelected>>", lambda e: self.preset_changed())

        ttk.Label(self.top, text="or search").pack(side="left", padx=(10, 0))
        en = ttk.Entry(self.top, textvariable=self.search, width=16)
        en.pack(side="left", padx=4)
        en.bind("<Return>", lambda ev: self.search_series(self.search.get().strip()))
        ttk.Button(self.top, text="Search",
                   command=lambda: self.search_series(self.search.get().strip())).pack(side="left")
        ttk.Button(self.top, text="Reload", command=self.reload).pack(side="left", padx=6)

        ttk.Label(self.top2, text="Series / season").pack(side="left")
        self.series_box = ttk.Combobox(self.top2, textvariable=self.series_name, state="readonly", width=70)
        self.series_box.pack(side="left", padx=4)
        self.series_box.bind("<<ComboboxSelected>>", lambda ev: self.pick_series())
        ttk.Label(self.top2, text="Click a match for a summary, double-click for the scorecard."
                  ).pack(side="left", padx=10)

        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self.on_scorecard)

    def show_usage(self):
        self.banner.config(text=API_USAGE["text"])

    # ---- navigation
    def preset_changed(self):
        term = TOURNAMENTS[self.preset.get()]
        if term is None:
            self.series_mode = False
            self.series_box["values"] = []
            self.series_name.set("")
            self.refresh()
        else:
            self.search_series(term)

    def reload(self):
        if self.series_mode and self.series_box.current() >= 0:
            self.pick_series()
        else:
            self.series_mode = False
            self.refresh()

    def refresh(self):
        """Auto-refresh only reloads the live list (cached 5 min to save API calls)."""
        if self.series_mode:
            return

        def work():
            return api("currentMatches", ttl=300).get("data", [])
        self.run(work, self._current_loaded)

    def _current_loaded(self, matches):
        live = [m for m in matches if m.get("matchStarted") and not m.get("matchEnded")]
        soon = [m for m in matches if not m.get("matchStarted")]
        done = [m for m in matches if m.get("matchEnded")]
        parts = []
        for title, group in (("LIVE", live), ("UPCOMING", soon), ("FINISHED", done)):
            if group:
                parts.append("---- " + title + " ----" + NL + (NL + NL).join(match_text(m) for m in group))
        self.set_text(self.right, (NL + NL).join(parts) or "No current matches.")
        if not self.series_mode:
            self.fill_matches(matches)
        self.show_usage()

    def fill_matches(self, matches):
        self.rows = sorted(matches, key=lambda m: str(m.get("dateTimeGMT") or m.get("date") or ""))
        table = []
        for m in self.rows:
            when = str(m.get("dateTimeGMT") or m.get("date") or "")[:10]
            table.append((when, m.get("name", ""), m.get("status", "")))
        if not table:
            table = [("", "No matches found", "")]
        self.show_table(("Date", "Match", "Result / Status"), table,
                        {"Date": 90, "Match": 400, "Result / Status": 380})
        self.show_usage()

    # ---- series
    def search_series(self, term):
        self.series_mode = True

        def work():
            data = api("series", ("&search=" + term) if term else "", ttl=600).get("data", [])
            return sorted(data, key=lambda s: str(s.get("startDate", "")), reverse=True)
        self.run(work, self._series_loaded)

    def _series_loaded(self, data):
        self.series = data
        self.series_box["values"] = [s.get("name", "") + "   (" + str(s.get("startDate", ""))[:10] + ")"
                                     for s in data]
        if data:
            self.series_box.current(0)
            self.pick_series()
        else:
            self.series_name.set("")
            self.fill_matches([])
            self.set_text(self.left, "No tournaments found for that search." + NL +
                          "Try a shorter or different word.")

    def pick_series(self):
        i = self.series_box.current()
        if i < 0:
            return
        sid = self.series[i]["id"]
        self.series_mode = True

        def work():
            return api("series_info", "&id=" + sid, ttl=600).get("data", {}).get("matchList", [])
        self.run(work, self.fill_matches)

    # ---- match detail
    def _picked(self):
        sel = self.tree.selection()
        if not sel:
            return None
        i = int(sel[0])
        return self.rows[i] if i < len(self.rows) else None

    def on_select(self, event):
        m = self._picked()
        if m:
            self.set_text(self.left, match_text(m) + NL + NL + "(double-click for the full scorecard)")

    def on_scorecard(self, event):
        m = self._picked()
        if not m or not m.get("id"):
            return
        mid = m["id"]

        def work():
            try:
                return scorecard_text(api("match_scorecard", "&id=" + mid, ttl=600).get("data", {}))
            except FetchError as e:
                info = api("match_info", "&id=" + mid, ttl=600).get("data", {})
                return match_text(info) + NL + NL + "(Full scorecard not available on this plan: " + str(e) + ")"
        self.run(work, lambda t: self.set_text(self.left, t))


# ---------------------------------------------------------------- F1 race map (latest session)
def f1_map_data():
    """Build track outline + latest car positions from OpenF1 location data."""
    s = get_json(OPENF1 + "/sessions?session_key=latest", ttl=20)[-1]
    key = str(s["session_key"])
    start, end = parse_dt(s["date_start"]), parse_dt(s["date_end"])
    is_live = start <= utcnow() <= end
    ref = utcnow() if is_live else end
    fmt = "%Y-%m-%dT%H:%M:%S"

    drivers = get_json(OPENF1 + "/drivers?session_key=" + key, ttl=600)
    info = {d["driver_number"]: d for d in drivers}

    a = (ref - timedelta(minutes=3)).strftime(fmt)
    b = ref.strftime(fmt)
    first = str(drivers[0]["driver_number"])
    outline = get_json(OPENF1 + "/location?session_key=" + key + "&driver_number=" + first +
                       "&date>" + a + "&date<" + b, ttl=10)
    outline_pts = [(p["x"], p["y"]) for p in outline]

    a2 = (ref - timedelta(seconds=10)).strftime(fmt)
    pos = get_json(OPENF1 + "/location?session_key=" + key + "&date>" + a2 + "&date<" + b, ttl=5)
    latest = {}
    for p in pos:
        latest[p["driver_number"]] = p
    cars = []
    for num, p in latest.items():
        d = info.get(num, {})
        cars.append((d.get("name_acronym", str(num)), p["x"], p["y"], "#" + d.get("team_colour", "ffffff")))
    label = (s["session_name"] + " - " + s["circuit_short_name"] + " (" +
             ("LIVE" if is_live else "replay of last moments") + ")")
    return {"outline": outline_pts, "cars": cars, "label": label, "live": is_live}


class RaceMap(tk.Toplevel):
    W, H = 760, 540

    def __init__(self, parent):
        super().__init__(parent)
        self.title("F1 Race Map (latest session)")
        self.canvas = tk.Canvas(self, width=self.W, height=self.H, bg="#101018")
        self.canvas.pack()
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left", padx=6, pady=4)
        self.lbl = ttk.Label(bar, text="")
        self.lbl.pack(side="left")
        self.q = queue.Queue()
        self.alive = True
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh()
        self._poll()

    def _close(self):
        self.alive = False
        self.destroy()

    def refresh(self):
        if not self.alive:
            return
        self.lbl.config(text="Loading...")

        def work():
            try:
                self.q.put(("ok", f1_map_data()))
            except Exception as e:
                self.q.put(("err", str(e) if isinstance(e, FetchError) else type(e).__name__ + ": " + str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        if not self.alive:
            return
        try:
            kind, p = self.q.get_nowait()
            if kind == "ok":
                self.draw(p)
                if p["live"]:
                    self.after(8000, self.refresh)
            else:
                self.lbl.config(text="Error: " + p[:100])
        except queue.Empty:
            pass
        self.after(200, self._poll)

    def draw(self, d):
        c = self.canvas
        c.delete("all")
        pts = d["outline"] + [(x, y) for _, x, y, _ in d["cars"]]
        if not pts:
            self.lbl.config(text="No position data returned for this session")
            return
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        scale = min((self.W - 60) / max(maxx - minx, 1), (self.H - 60) / max(maxy - miny, 1))

        def tx(x):
            return 30 + (x - minx) * scale

        def ty(y):
            return self.H - 30 - (y - miny) * scale

        if len(d["outline"]) > 1:
            flat = [v for x, y in d["outline"] for v in (tx(x), ty(y))]
            c.create_line(*flat, fill="#555566", width=8, capstyle="round", joinstyle="round")
        for name, x, y, col in d["cars"]:
            c.create_oval(tx(x) - 6, ty(y) - 6, tx(x) + 6, ty(y) + 6, fill=col, outline="white")
            c.create_text(tx(x) + 10, ty(y) - 10, text=name, fill="white", font=("Arial", 8, "bold"))
        self.lbl.config(text=d["label"] + "  -  " + time.strftime("%H:%M:%S"))


# ---------------------------------------------------------------- Diagnostics
class DiagTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Run self-test", command=self.selftest).pack(side="left")
        ttk.Button(bar, text="Copy log", command=self.copy).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left")
        ttk.Label(bar, text="If anything shows an error: press Run self-test, then Copy log "
                            "and send me the text (your API key is hidden).").pack(side="left", padx=10)
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.text = tk.Text(box, font=("Consolas", 9), wrap="none")
        sy = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        sx = ttk.Scrollbar(box, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.text.pack(fill="both", expand=True)
        self.sig = None
        self._tick()

    def ensure_loaded(self):
        pass

    def refresh(self):
        pass

    def _tick(self):
        with LOG_LOCK:
            lines = list(LOG)
        sig = (len(lines), lines[-1] if lines else "")
        if sig != self.sig:
            self.sig = sig
            self.text.delete("1.0", "end")
            self.text.insert("end", NL.join(lines))
            self.text.see("end")
        self.after(1000, self._tick)

    def copy(self):
        with LOG_LOCK:
            data = NL.join(LOG)
        self.clipboard_clear()
        self.clipboard_append(data)

    def clear(self):
        with LOG_LOCK:
            LOG.clear()

    def selftest(self):
        def work():
            log("---- SELF-TEST START ----")
            today = utcnow().date()
            a = today - timedelta(days=30)
            sb = ESPN + "/site/v2/sports/soccer/"
            tests = [
                ("F1 results source (Jolpica)", JOLPICA + "/current.json?limit=1"),
                ("F1 live source (OpenF1)", OPENF1 + "/sessions?session_key=latest"),
                ("Football table (eng.1)", ESPN + "/v2/sports/soccer/eng.1/standings"),
                ("Football one day (eng.1)", sb + "eng.1/scoreboard?dates=" + today.strftime("%Y%m%d")),
                ("Football 30-day range (Champions League)",
                 sb + "uefa.champions/scoreboard?dates=" + a.strftime("%Y%m%d") + "-" +
                 today.strftime("%Y%m%d") + "&limit=300"),
                ("Cricket (CricAPI currentMatches)",
                 CRICAPI + "/currentMatches?apikey=" + CRICAPI_KEY + "&offset=0"),
            ]
            for name, url in tests:
                try:
                    d = get_json(url, ttl=0)
                    if name.startswith("Cricket") and isinstance(d, dict) and d.get("status") != "success":
                        log("TEST FAIL  " + name + ": CricAPI said " + str(d.get("status")) +
                            " - " + str(d.get("reason", "")))
                    else:
                        log("TEST PASS  " + name)
                except Exception as e:
                    log("TEST FAIL  " + name + ": " + str(e))
            log("---- SELF-TEST END ----")
        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- main
def main():
    root = tk.Tk()
    root.title("Sports Hub")
    root.geometry("1150x800")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    tabs = [F1Tab(nb, root), FootballTab(nb), CricketTab(nb), DiagTab(nb)]
    for tab, name in zip(tabs, ("  Formula 1  ", "  Football  ", "  Cricket  ", "  Diagnostics  ")):
        nb.add(tab, text=name)

    nb.bind("<<NotebookTabChanged>>", lambda e: tabs[nb.index("current")].ensure_loaded())
    root.after(300, tabs[0].ensure_loaded)

    def auto():
        tabs[nb.index("current")].refresh()
        root.after(AUTO_REFRESH_SECONDS * 1000, auto)
    root.after(AUTO_REFRESH_SECONDS * 1000, auto)

    root.mainloop()


if __name__ == "__main__":
    main()
