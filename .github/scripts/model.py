"""Poissonmodell som sätter en rad utan att se odds eller streck.

Räknas i arbetsflödet i stället för i webbläsaren, av två skäl. Den fångas
vid stopptid även om appen aldrig öppnas, och det finns bara en uträkning
att underhålla i stället för två som kan glida isär.

Efter stopptid slutar filen uppdateras, så det som står kvar är modellens
åsikt innan matcherna spelades.
"""

import json
import math
import os
from datetime import datetime, timezone

OUT = "model.json"
HALF_LIFE = 90        # dygn innan en match väger hälften
PRIOR = 6             # matcher av liga-snitt som blandas in vid tunt underlag
MAX_GOALS = 8
SIGNS = ["one", "x", "two"]


def load(path, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return fallback


def read_draw():
    payload = load("data.json", {})
    draw = payload.get("draw") or {}
    if isinstance(draw, dict) and "draws" in draw:
        draws = draw.get("draws") or []
        if not draws:
            return None
        draw = draws[0]
    return draw


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def all_games(football):
    games = []
    for rows in (football.get("results") or {}).values():
        games.extend(rows or [])
    games.extend(football.get("past") or [])
    return [g for g in games
            if g.get("hg") is not None and g.get("ag") is not None and g.get("d")]


def build(games):
    """Anfalls- och försvarsstyrka per lag, uppdelat på hemma och borta."""
    now = datetime.now(timezone.utc)

    def weight(when):
        stamp = parse_time(when)
        if not stamp:
            return 0.0
        days = (now - stamp).total_seconds() / 86400.0
        if days < 0:
            return 0.0
        return 0.5 ** (days / HALF_LIFE)

    wsum = hgoals = agoals = 0.0
    for g in games:
        w = weight(g["d"])
        wsum += w
        hgoals += w * g["hg"]
        agoals += w * g["ag"]

    if wsum <= 0:
        return None

    avg_home = hgoals / wsum
    avg_away = agoals / wsum

    # Egna tabeller för hemma och borta. Vissa lag är helt olika beroende
    # på plan, och ett gemensamt snitt döljer det.
    teams = {}

    def bump(team_id, side, scored, conceded, w):
        t = teams.setdefault(team_id, {
            "home": {"gf": 0.0, "ga": 0.0, "w": 0.0},
            "away": {"gf": 0.0, "ga": 0.0, "w": 0.0},
        })
        t[side]["gf"] += w * scored
        t[side]["ga"] += w * conceded
        t[side]["w"] += w

    for g in games:
        w = weight(g["d"])
        bump(g["h"], "home", g["hg"], g["ag"], w)
        bump(g["a"], "away", g["ag"], g["hg"], w)

    strength = {}
    for team_id, t in teams.items():
        def ratio(side, key, league_avg):
            box = t[side]
            blended = (box[key] + PRIOR * league_avg) / (box["w"] + PRIOR)
            return blended / league_avg if league_avg else 1.0

        strength[team_id] = {
            "attackHome": ratio("home", "gf", avg_home),
            "defenceHome": ratio("home", "ga", avg_away),
            "attackAway": ratio("away", "gf", avg_away),
            "defenceAway": ratio("away", "ga", avg_home),
            "games": t["home"]["w"] + t["away"]["w"],
        }

    return {"strength": strength, "avgHome": avg_home, "avgAway": avg_away,
            "sample": len(games)}


def poisson(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def outcome(model, home_id, away_id):
    sh = model["strength"].get(home_id)
    sa = model["strength"].get(away_id)
    if not sh or not sa or sh["games"] < 1 or sa["games"] < 1:
        return None

    lam_home = model["avgHome"] * sh["attackHome"] * sa["defenceAway"]
    lam_away = model["avgAway"] * sa["attackAway"] * sh["defenceHome"]

    one = x = two = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            pr = poisson(i, lam_home) * poisson(j, lam_away)
            if i > j:
                one += pr
            elif i == j:
                x += pr
            else:
                two += pr

    total = one + x + two
    if total <= 0:
        return None

    probs = {"one": one / total * 100, "x": x / total * 100, "two": two / total * 100}
    sign = max(SIGNS, key=lambda k: probs[k])
    return {"p": {k: round(probs[k], 1) for k in SIGNS},
            "lh": round(lam_home, 2), "la": round(lam_away, 2),
            "sign": sign}


# Lagnamn skiljer sig mellan källorna, samma tolerans som i appen.
def norm(text):
    import unicodedata
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    for word in ("fc", "afc", "cf", "sk", "if", "ff", "bk"):
        text = text.replace(" " + word + " ", " ")
    return "".join(c for c in text if c.isalnum())


def team_lookup(football):
    """Karta från normaliserat namn och lagkod till lag-id."""
    by_name, by_tla = {}, {}
    for comp in (football.get("competitions") or {}).values():
        for group in (comp.get("standings") or []):
            for row in group.get("table") or []:
                team = row.get("team") or {}
                tid = team.get("id")
                if not tid:
                    continue
                for name in (team.get("name"), team.get("shortName")):
                    if name:
                        by_name[norm(name)] = tid
                if team.get("tla"):
                    by_tla[team["tla"].upper()] = tid
    return by_name, by_tla


def find_team(part, by_name, by_tla):
    tla = (part.get("shortName") or "").upper()
    if tla and tla in by_tla:
        return by_tla[tla]

    key = norm(part.get("name"))
    if key in by_name:
        return by_name[key]

    for name, tid in by_name.items():
        if key and (name.startswith(key) or key.startswith(name)):
            return tid
    for name, tid in by_name.items():
        if key and len(key) >= 4 and (key in name or name in key):
            return tid
    return None


def main():
    draw = read_draw()
    if not draw:
        print("Ingen öppen omgång, modellen hoppas över.")
        return

    draw_number = draw.get("drawNumber")
    previous = load(OUT, {})

    # Efter stopptid ska filen inte röras. Då står modellens åsikt kvar
    # som den var innan matcherna spelades.
    close = parse_time(draw.get("regCloseTime"))
    if close and datetime.now(timezone.utc) > close:
        if str(previous.get("drawNumber")) == str(draw_number):
            print("Omgång %s har stängt, modellen lämnas orörd." % draw_number)
            return

    football = load("football.json", {})
    games = all_games(football)
    if len(games) < 20:
        print("För få spelade matcher (%d), modellen hoppas över." % len(games))
        return

    model = build(games)
    if not model:
        print("Kunde inte bygga modellen.")
        return

    by_name, by_tla = team_lookup(football)
    picks, detail, missing = {}, {}, []

    for event in draw.get("drawEvents", []):
        if event.get("eventTypeId") == 2:
            continue
        match = event.get("match") or {}
        key = str(match.get("matchId") or event.get("eventNumber") or "")
        parts = match.get("participants") or []
        home = next((p for p in parts if p.get("type") == "home"), None)
        away = next((p for p in parts if p.get("type") == "away"), None)
        if not key or not home or not away:
            continue

        hid = find_team(home, by_name, by_tla)
        aid = find_team(away, by_name, by_tla)
        if hid is None or aid is None:
            missing.append("%s – %s" % (home.get("name"), away.get("name")))
            continue

        res = outcome(model, hid, aid)
        if not res:
            missing.append("%s – %s" % (home.get("name"), away.get("name")))
            continue

        picks[key] = res["sign"]
        detail[key] = dict(res, home=home.get("name"), away=away.get("name"))

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"drawNumber": draw_number,
                   "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "sample": model["sample"],
                   "avgHome": round(model["avgHome"], 3),
                   "avgAway": round(model["avgAway"], 3),
                   "picks": picks, "detail": detail},
                  fh, ensure_ascii=False, indent=1)

    print("Modellen satte %d av %d tecken. Underlag %d matcher." %
          (len(picks), len(picks) + len(missing), model["sample"]))
    for name in missing:
        print("   Saknar underlag: %s" % name)


if __name__ == "__main__":
    main()
