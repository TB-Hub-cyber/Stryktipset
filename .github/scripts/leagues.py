"""Läser veckans kupong och avgör vilka ligor som ska hämtas.

Stryktipset plockar matcher ur olika serier varje omgång. I stället för att
alltid hämta samma två ligor läser vi vilka som faktiskt är med, och hämtar
bara dem. Ligor utanför football-data.org:s gratisnivå hoppas över.
"""

import json
import sys

# Nyckeln är (land, ord som ska finnas i ligans namn). Landet behövs för att
# skilja italienska Serie A från brasilianska.
MAPPING = [
    (("england",),  ("premier league",),                 "PL"),
    (("england",),  ("championship",),                    "ELC"),
    (("spain", "spanien"), ("liga",),                     "PD"),
    (("italy", "italien"), ("serie a",),                  "SA"),
    (("brazil", "brasilien"), ("serie a", "série a"),     "BSA"),
    (("germany", "tyskland"), ("bundesliga",),            "BL1"),
    (("france", "frankrike"), ("ligue 1",),               "FL1"),
    (("netherlands", "nederländerna", "holland"), ("eredivisie",), "DED"),
    (("portugal",), ("primeira", "liga portugal"),        "PPL"),
    (("europe", "europa"), ("champions league",),         "CL"),
    (("world", "världen"), ("world cup", "vm"),           "WC"),
    (("europe", "europa"), ("european championship", "em"), "EC"),
]


def read_events():
    with open("data.json", encoding="utf-8") as fh:
        payload = json.load(fh)

    draw = payload.get("draw") or {}
    if isinstance(draw, dict) and "draws" in draw:
        draw = draw["draws"][0]
    return draw.get("drawEvents", [])


def code_for(league_name, country_name):
    name = (league_name or "").lower()
    country = (country_name or "").lower()

    for countries, words, code in MAPPING:
        if not any(c in country for c in countries):
            continue
        if any(w in name for w in words):
            return code
    return None


def main():
    codes = []
    skipped = []

    for event in read_events():
        league = (event.get("match") or {}).get("league") or {}
        name = league.get("name")
        country = ((league.get("country") or {}).get("name")) or ""

        code = code_for(name, country)
        if code:
            if code not in codes:
                codes.append(code)
        elif name and name not in skipped:
            skipped.append(name)

    if not codes:
        # Hellre de vanligaste två än ingenting alls.
        codes = ["PL", "ELC"]
        print("Hittade inga kända ligor, faller tillbaka på PL och ELC.", file=sys.stderr)

    for name in skipped:
        print("Utanför gratisnivån, hoppas över: %s" % name, file=sys.stderr)

    print("Hämtar: %s" % ", ".join(codes), file=sys.stderr)
    print(" ".join(codes))


if __name__ == "__main__":
    main()
