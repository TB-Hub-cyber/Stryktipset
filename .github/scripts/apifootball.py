"""Hämtar det football-data.org inte täcker, via API-Football.

Gäller League One och League Two, Allsvenskan och Superettan, landskamper —
och dessutom skador samt cupmatcher för att bedöma matchtäthet.

Gratisnivån ger 100 anrop per dygn. Skriptet kör därför bara vissa timmar,
cachar liga-id mellan körningar och avbryter när kvoten börjar ta slut.
"""

import json
import os
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

OUT = "af-extra.json"
BASE = "https://v3.football.api-sports.io"
KEY = os.environ.get("API_FOOTBALL_KEY", "")
FORCED = os.environ.get("FORCE_APIFOOTBALL") == "1"

# Tabeller och skador ändras långsamt. Fyra gånger per dygn räcker gott,
# och håller förbrukningen långt under kvoten.
RUN_HOURS = (4, 10, 16, 22)
MIN_REMAINING = 12

# Cuper som avgör matchtäthet för engelska lag. Hämtas som datumfönster,
# inte per lag, vilket håller nere antalet anrop.
CUPS = [
    ("UEFA Europa League", "World"),
    ("UEFA Europa Conference League", "World"),
    ("UEFA Champions League", "World"),
    ("FA Cup", "England"),
    ("League Cup", "England"),
]

ALREADY_COVERED = (
    "premier league", "championship", "la liga", "serie a", "bundesliga",
    "ligue 1", "eredivisie", "primeira", "champions league",
)

WINDOW_BACK, WINDOW_FWD = 12, 12     # dygn bakåt och framåt för matchtäthet


def norm(text):
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return "".join(c for c in text if c.isalnum())


def call(path, params, state):
    if state["remaining"] is not None and state["remaining"] < MIN_REMAINING:
        raise RuntimeError("kvoten nästan slut")

    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "x-apisports-key": KEY, "Accept": "application/json"})

    with urllib.request.urlopen(req, timeout=30) as resp:
        left = resp.headers.get("x-ratelimit-requests-remaining")
        body = json.load(resp)

    try:
        state["remaining"] = int(left)
    except (TypeError, ValueError):
        pass

    errors = body.get("errors")
    if errors:
        print("Fel från API-Football: %s" % errors, file=sys.stderr)

    return body.get("response") or []


def coupon_leagues(only_uncovered):
    """Ligor i kupongen. Skador hämtas för alla, tabeller bara för otäckta."""
    with open("data.json", encoding="utf-8") as fh:
        payload = json.load(fh)

    draw = payload.get("draw") or {}
    if isinstance(draw, dict) and "draws" in draw:
        draw = draw["draws"][0]

    found = []
    for event in draw.get("drawEvents", []):
        league = (event.get("match") or {}).get("league") or {}
        name = (league.get("name") or "").strip()
        country = ((league.get("country") or {}).get("name") or "").strip()
        if not name:
            continue
        if only_uncovered and any(w in name.lower() for w in ALREADY_COVERED):
            continue
        if (name, country) not in found:
            found.append((name, country))
    return found


def load_previous():
    if not os.path.exists(OUT):
        return {}
    try:
        with open(OUT, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (ValueError, OSError):
        return {}


def resolve(name, country, cache, state):
    """Liga-id och pågående säsong. Cachas — uppslagningen kostar ett anrop.

    Tar både serier och cuper, till skillnad från tidigare. Landskamper och
    cupspel ligger som typ Cup och skulle annars filtreras bort.
    """
    key = "%s|%s" % (name, country)
    if key in cache:
        return cache[key]

    params = {"search": name}
    if country and country.lower() != "world":
        params["country"] = country

    rows = call("/leagues", params, state)
    target = norm(name)
    best = None

    for row in rows:
        league = row.get("league") or {}
        current = None
        for season in row.get("seasons") or []:
            if season.get("current"):
                current = season.get("year")
        if current is None:
            continue

        candidate = norm(league.get("name"))
        # Exakt namn slår delträff, så "Premier League - Summer Series"
        # aldrig vinner över "Premier League".
        score = 2 if candidate == target else 1 if target in candidate else 0
        if score and (best is None or score > best[0]):
            best = (score, {"id": league.get("id"), "season": current,
                            "name": league.get("name"),
                            "type": league.get("type")})

    if not best:
        print("Ingen pågående säsong för %s (%s)." % (name, country), file=sys.stderr)
        return None

    cache[key] = best[1]
    return best[1]


def standings_for(hit, state):
    rows = call("/standings", {"league": hit["id"], "season": hit["season"]}, state)
    if not rows:
        return None

    groups = ((rows[0].get("league") or {}).get("standings")) or []
    table = []
    for group in groups:
        for entry in group:
            team = entry.get("team") or {}
            total = entry.get("all") or {}
            goals = total.get("goals") or {}
            table.append({
                "position": entry.get("rank"),
                "team": {"id": 900000 + int(team.get("id") or 0),
                         "name": team.get("name"),
                         "shortName": team.get("name"), "tla": None},
                "playedGames": total.get("played") or 0,
                "won": total.get("win") or 0,
                "draw": total.get("draw") or 0,
                "lost": total.get("lose") or 0,
                "points": entry.get("points") or 0,
                "goalsFor": goals.get("for") or 0,
                "goalsAgainst": goals.get("against") or 0,
                "goalDifference": entry.get("goalsDiff") or 0,
            })

    if not table:
        return None

    return {"competition": {"id": hit["id"], "name": hit["name"],
                            "code": "AF%s" % hit["id"]},
            "standings": [{"stage": "REGULAR_SEASON", "type": "TOTAL", "table": table}]}


def matchday(row):
    for part in ((row.get("league") or {}).get("round") or "").split():
        if part.isdigit():
            return int(part)
    return None


def slim(row, code, played_only):
    fixture = row.get("fixture") or {}
    teams = row.get("teams") or {}
    goals = row.get("goals") or {}
    half = (row.get("score") or {}).get("halftime") or {}
    home, away = teams.get("home") or {}, teams.get("away") or {}

    done = goals.get("home") is not None and goals.get("away") is not None
    if played_only and not done:
        return None

    return {
        "d": fixture.get("date"), "c": code, "md": matchday(row),
        "h": 900000 + int(home.get("id") or 0),
        "a": 900000 + int(away.get("id") or 0),
        "hn": home.get("name"), "an": away.get("name"),
        "hg": goals.get("home") if done else None,
        "ag": goals.get("away") if done else None,
        "hh": half.get("home"), "ah": half.get("away"),
    }


def results_for(hit, state):
    code = "AF%s" % hit["id"]
    rows = call("/fixtures", {"league": hit["id"], "season": hit["season"],
                              "status": "FT"}, state)
    return [r for r in (slim(x, code, True) for x in rows) if r]


def window_for(hit, state):
    """Matcher runt idag — bakåt för trötthet, framåt för rotationsrisk."""
    today = datetime.now(timezone.utc).date()
    code = "AF%s" % hit["id"]
    rows = call("/fixtures", {
        "league": hit["id"], "season": hit["season"],
        "from": str(today - timedelta(days=WINDOW_BACK)),
        "to": str(today + timedelta(days=WINDOW_FWD)),
    }, state)
    return [r for r in (slim(x, code, False) for x in rows) if r]


def injuries_for(hit, state):
    """Nycklas på normaliserat lagnamn, inte id.

    Premier League-lagen kommer från football-data med andra id, så id skulle
    bara fungera för de ligor vi hämtar härifrån. Namn matchar överallt.
    """
    rows = call("/injuries", {"league": hit["id"], "season": hit["season"]}, state)
    out = {}
    for row in rows:
        team = (row.get("team") or {}).get("name")
        player = row.get("player") or {}
        if not team or not player.get("name"):
            continue
        out.setdefault(norm(team), []).append({
            "team": team,
            "n": player.get("name"),
            "t": player.get("type"),        # Missing Fixture / Questionable
            "r": player.get("reason"),
        })
    return out


def main():
    if not KEY:
        print("API_FOOTBALL_KEY saknas, hoppar över.", file=sys.stderr)
        return

    hour = datetime.now(timezone.utc).hour
    previous = load_previous()

    if not FORCED and hour not in RUN_HOURS:
        print("Klockan %02d UTC är utanför hämtfönstret %s — behåller förra "
              "hämtningen." % (hour, RUN_HOURS))
        return

    cache = previous.get("lookup") or {}
    state = {"remaining": None}

    competitions = dict(previous.get("competitions") or {})
    results = dict(previous.get("results") or {})
    upcoming = list(previous.get("upcoming") or [])
    injuries = dict(previous.get("injuries") or {})

    try:
        uncovered = coupon_leagues(True)
        fresh_up = []

        # Tabeller och resultat: bara ligor football-data saknar.
        for name, country in uncovered:
            hit = resolve(name, country, cache, state)
            if not hit:
                continue
            code = "AF%s" % hit["id"]

            table = standings_for(hit, state)
            if table:
                competitions[code] = table

            results[code] = results_for(hit, state)
            print("%s: tabell %s, %d spelade matcher." % (
                hit["name"], "ja" if table else "nej", len(results[code])))

        # Skador och matchfönster: alla ligor i kupongen.
        injuries = {}
        for name, country in coupon_leagues(False):
            hit = resolve(name, country, cache, state)
            if not hit:
                continue
            injuries.update(injuries_for(hit, state))
            fresh_up.extend(window_for(hit, state))
            print("%s: skador och matchfönster hämtade." % hit["name"])

        upcoming = fresh_up

        # Cupspel avgör matchtäthet men syns inte i seriematcherna.
        for name, country in CUPS:
            hit = resolve(name, country, cache, state)
            if not hit:
                continue
            rows = window_for(hit, state)
            upcoming.extend(rows)
            print("%s: %d matcher i fönstret." % (hit["name"], len(rows)))

    except RuntimeError as exc:
        print("Avbryter: %s (%s kvar)" % (exc, state["remaining"]), file=sys.stderr)
    except Exception as exc:
        print("Fel under hämtning: %s" % exc, file=sys.stderr)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"lookup": cache, "competitions": competitions,
                   "results": results, "upcoming": upcoming,
                   "injuries": injuries},
                  fh, ensure_ascii=False, separators=(",", ":"))

    print("Anrop kvar i dygnskvoten: %s" % state["remaining"])


if __name__ == "__main__":
    main()
