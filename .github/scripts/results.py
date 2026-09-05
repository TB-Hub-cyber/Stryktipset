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
RESULT_API = "https://api.spela.svenskaspel.se/draw/1/stryktipset/draws/%s/result"
OUTCOMES = ["one", "x", "two"]


def fetch(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "stryktips-hobbyprojekt",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


SIGNS = {"1": "one", "X": "x", "2": "two"}


def read_result(draw_number):
    """Facit ligger på en egen adress som publiceras när omgången rättats.

    Varje match har outcome som 1, X eller 2, och outcomeScore med målen.
    Där finns även utdelningen per vinstgrupp.
    """
    payload = fetch(RESULT_API % draw_number)
    result = payload.get("result") or {}
    events = result.get("events") or []

    facit, scores = {}, {}
    for event in events:
        key = str(event.get("matchId") or event.get("eventNumber") or "")
        sign = SIGNS.get(str(event.get("outcome") or "").strip().upper())
        if not key or not sign:
            continue
        facit[key] = sign
        score = event.get("outcomeScore") or {}
        if score.get("home") is not None and score.get("away") is not None:
            scores[key] = {"h": score["home"], "a": score["away"]}

    payouts = []
    for row in result.get("distribution") or []:
        payouts.append({"name": row.get("name"),
                        "winners": row.get("winners"),
                        "amount": row.get("amount")})

    return facit, scores, payouts


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


def load_model(draw_number):
    """Modellens tecken för omgången, om de hann sparas före stopptid."""
    if not os.path.exists("model.json"):
        return None
    try:
        with open("model.json", encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        return None
    if str(data.get("drawNumber")) != str(draw_number):
        return None
    return data.get("picks") or None


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
            facit, scores, payouts = read_result(number)
        except Exception as exc:
            print("Facit för omgång %s inte publicerat än (%s)." % (number, exc))
            continue

        if not facit:
            print("Omgång %s: facit tomt, försöker igen nästa körning." % number)
            continue

        picks = entry.get("picks") or {}
        correct = sum(1 for key, sel in picks.items()
                      if facit.get(key) in (sel or []))

        closing = closing_values(number, entry.get("closeTime"))

        # Modellens rad låstes vid stopptid av model.py.
        if not entry.get("model"):
            saved = load_model(number)
            if saved:
                entry["model"] = saved

        if entry.get("model"):
            hits = sum(1 for k, v in entry["model"].items() if facit.get(k) == v)
            entry["modelCorrect"] = hits
            print("Modellen fick %d rätt." % hits)

        entry["result"] = facit
        entry["scores"] = scores
        entry["payouts"] = payouts
        entry["closing"] = closing
        entry["correct"] = correct
        entry["gradedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry.pop("rawSample", None)
        changed = True

        print("Omgång %s rättad: %d rätt av %d." % (number, correct, len(facit)))
        for row in payouts[:2]:
            print("  %s: %s vinnare, %s kr" %
                  (row["name"], row["winners"], row["amount"]))
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
