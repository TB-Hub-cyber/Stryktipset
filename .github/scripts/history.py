"""Sparar odds och streck vid varje hämtning, så rörelsen kan mätas från start.

Svenska Spels refDistribution ser ut att förnyas löpande och duger därför inte
som fast startpunkt. Den här filen bygger en egen historik i stället.
"""

import json
import os

HIST = "history.json"
MAX_SNAPSHOTS = 400          # räcker till en omgång med god marginal
OUTCOMES = {"1": 0, "X": 1, "2": 2}


def as_number(value):
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def read_draw():
    """Mellan stopptid och nästa öppning svarar Svenska Spel med en tom lista."""
    with open("data.json", encoding="utf-8") as fh:
        payload = json.load(fh)

    draw = payload.get("draw") or {}
    if isinstance(draw, dict) and "draws" in draw:
        draws = draw.get("draws") or []
        if not draws:
            return payload.get("fetchedAt"), None
        draw = draws[0]
    return payload.get("fetchedAt"), draw


def build_snapshot(fetched_at, draw):
    matches = {}

    for event in draw.get("drawEvents", []):
        if event.get("eventTypeId") == 2:
            continue

        match = event.get("match") or {}
        key = str(match.get("matchId") or event.get("eventNumber") or "")
        if not key:
            continue

        odds = event.get("odds") or {}
        streck = [None, None, None]

        metrics = event.get("betMetrics") or {}
        for value in metrics.get("values") or []:
            index = OUTCOMES.get(str(value.get("outcome")).upper())
            if index is None:
                continue
            distribution = value.get("distribution") or {}
            streck[index] = as_number(distribution.get("distribution"))

        matches[key] = {
            "o": [as_number(odds.get("one")),
                  as_number(odds.get("x")),
                  as_number(odds.get("two"))],
            "d": streck,
        }

    return {"t": fetched_at, "m": matches}


def load_history(draw_number):
    if not os.path.exists(HIST):
        return None

    try:
        with open(HIST, encoding="utf-8") as fh:
            history = json.load(fh)
    except (ValueError, OSError):
        return None

    # Ny omgång betyder att historiken börjar om från noll.
    if history.get("drawNumber") != draw_number:
        return None

    return history


def main():
    fetched_at, draw = read_draw()
    if not draw:
        print("Ingen öppen omgång just nu — historiken lämnas orörd.")
        return

    draw_number = draw.get("drawNumber")
    snapshot = build_snapshot(fetched_at, draw)

    if not snapshot["m"]:
        print("Inga matcher i datan, hoppar över historiken.")
        return

    history = load_history(draw_number)
    if history is None:
        history = {"drawNumber": draw_number, "snapshots": []}
        print("Ny omgång %s, historiken börjar om." % draw_number)

    snapshots = history["snapshots"]

    # Spara bara när något faktiskt rört sig, annars växer filen i onödan.
    if snapshots and snapshots[-1].get("m") == snapshot["m"]:
        print("Oförändrat sedan förra hämtningen, ingen ny post.")
        return

    snapshots.append(snapshot)
    history["snapshots"] = snapshots[-MAX_SNAPSHOTS:]

    with open(HIST, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, separators=(",", ":"))

    print("Sparade post %d för omgång %s." % (len(history["snapshots"]), draw_number))


if __name__ == "__main__":
    main()
