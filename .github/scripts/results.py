"""Hämtar facit för omgångar där en rad lämnats in.

Svenska Spel låter oss hämta en enskild omgång via dess nummer, även efter att
nästa kupong öppnat. Vi går igenom sparade rader, hämtar facit för dem som
stängt, och räknar antal rätt.

Fältet result är tomt före avspark, så exakt form är okänd tills en omgång
spelats. Skriptet provar därför flera tolkningar och sparar rådatan om ingen
fungerar — då går det att justera utan att något gått förlorat.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

ROWS = "rows.json"
HIST = "history.json"
API = "https://api.spela.svenskaspel.se/draw/1/stryktipset/draws/%s"
OUTCOMES = ["one", "x", "two"]


def fetch(draw_number):
    req = urllib.request.Request(
        API % draw_number,
        headers={"User-Agent": "stryktips-hobbyprojekt",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def outcome_from(event):
    """Försöker lista ut 1, X eller 2 ur en spelad match."""
    result = event.get("result")

    # Form A: lista med delresultat, fulltid sist eller markerat.
    if isinstance(result, list) and result:
        best = None
        for item in result:
            if not isinstance(item, dict):
                continue
            home = item.get("home", item.get("homeScore"))
            away = item.get("away", item.get("awayScore"))
            if home is None or away is None:
                continue
            kind = str(item.get("type", item.get("period", ""))).lower()
            if "full" in kind or "final" in kind:
                best = (home, away)
                break
            best = (home, away)
        if best:
            return sign(best[0], best[1])

    # Form B: färdigt tecken någonstans i eventet.
    for key in ("outcome", "winner", "sign"):
        value = event.get(key)
        if isinstance(value, str) and value.strip().upper() in ("1", "X", "2"):
            return {"1": "one", "X": "x", "2": "two"}[value.strip().upper()]

    # Form C: målen ligger direkt på matchen.
    match = event.get("match") or {}
    home = match.get("homeScore", match.get("home"))
    away = match.get("awayScore", match.get("away"))
    if isinstance(home, int) and isinstance(away, int):
        return sign(home, away)

    return None


def sign(home, away):
    home, away = int(home), int(away)
    return "one" if home > away else "two" if away > home else "x"


def closing_values(draw_number, close_time):
    """Odds och streck vid sista mätningen före stopptid.

    Historiken nollställs när en ny omgång öppnar, så värdena måste plockas ut
    och sparas tillsammans med facit — annars är de borta nästa vecka.
    """
    if not os.path.exists(HIST):
        return {}

    try:
        with open(HIST, encoding="utf-8") as fh:
            history = json.load(fh)
    except (ValueError, OSError):
        return {}

    if str(history.get("drawNumber")) != str(draw_number):
        print("Historiken gäller omgång %s, inte %s — hoppar över "
              "stängningsvärden." % (history.get("drawNumber"), draw_number))
        return {}

    limit = None
    if close_time:
        try:
            limit = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        except ValueError:
            limit = None

    chosen = None
    for snap in history.get("snapshots", []):
        when = snap.get("t")
        if limit and when:
            try:
                stamp = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            if stamp > limit:
                continue
        chosen = snap

    if not chosen:
        return {}

    out = {}
    for key, values in (chosen.get("m") or {}).items():
        odds = values.get("o") or [None, None, None]
        dist = values.get("d") or [None, None, None]
        out[key] = {
            "t": chosen.get("t"),
            "odds": dict(zip(OUTCOMES, odds)),
            "dist": dict(zip(OUTCOMES, dist)),
        }
    return out


def load_rows():
    if not os.path.exists(ROWS):
        return {"rounds": {}}
    try:
        with open(ROWS, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return {"rounds": {}}
    data.setdefault("rounds", {})
    return data


def has_closed(close_time):
    if not close_time:
        return True
    try:
        closed = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) > closed


def main():
    rows = load_rows()
    changed = False

    for number, entry in rows["rounds"].items():
        if entry.get("result"):
            continue
        if not has_closed(entry.get("closeTime")):
            print("Omgång %s har inte stängt än." % number)
            continue

        try:
            payload = fetch(number)
        except Exception as exc:                      # nätverk eller 404
            print("Kunde inte hämta omgång %s: %s" % (number, exc))
            continue

        draw = payload.get("draw") or payload
        if "draws" in draw:
            draw = draw["draws"][0]

        events = [e for e in draw.get("drawEvents", []) if e.get("eventTypeId") != 2]
        if not events:
            print("Omgång %s saknar matcher i svaret." % number)
            continue

        facit = {}
        raw = {}
        pending = 0

        for event in events:
            key = str((event.get("match") or {}).get("matchId") or event.get("eventNumber"))
            outcome = outcome_from(event)
            if outcome:
                facit[key] = outcome
            else:
                pending += 1
                raw[key] = event.get("result")

        if pending:
            print("Omgång %s: %d av %d matcher saknar facit än." %
                  (number, pending, len(events)))
            # Spara rådatan en gång så formen går att granska.
            if raw and not entry.get("rawSample"):
                entry["rawSample"] = raw
                changed = True
            continue

        picks = entry.get("picks") or {}
        correct = sum(1 for key, sel in picks.items()
                      if facit.get(key) in (sel or []))

        closing = closing_values(number, entry.get("closeTime"))

        entry["result"] = facit
        entry["closing"] = closing
        entry["correct"] = correct
        entry["gradedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry.pop("rawSample", None)
        changed = True

        print("Omgång %s rättad: %d rätt av %d." % (number, correct, len(events)))
        if closing:
            sample = next(iter(closing.values()))
            print("Stängningsvärden sparade från mätningen %s." % sample.get("t"))
        else:
            print("Inga stängningsvärden hittades i historiken.")

    if changed:
        with open(ROWS, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print("rows.json uppdaterad.")
    else:
        print("Inget att rätta.")


if __name__ == "__main__":
    main()
