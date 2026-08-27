"""Samlar lagnyheter inför omgången med hjälp av en språkmodell.

Modellen får söka på webben och sammanfatta skador, avstängningar och
laguppställningsbesked. Den får inte bedöma matcherna — siffrorna i appen
gör den analysen, och en modell som tycker till låter övertygande på fel
grunder.

Varje påstående måste ha en länk, och länken kontrolleras här innan den
sparas. En påhittad skada är sämre än ingen text alls.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OUT = "news.json"
SOURCES = "sources.json"
KEY = os.environ.get("OPENAI_API_KEY", "")
# En odefinierad repository-variabel blir tom sträng, inte frånvarande.
# Utan "or" skulle den tomma strängen slå ut standardnamnet.
MODEL = os.environ.get("NEWS_MODEL") or "gpt-4o-mini"
FORCED = os.environ.get("FORCE_NEWS") == "1"

API = "https://api.openai.com/v1/responses"
MAX_MATCHES = 13
MAX_ITEMS = 4              # per lag
LINK_TIMEOUT = 8

PROMPT = """Du samlar lagnyheter inför en fotbollsmatch. Du ska INTE bedöma
matchen, tippa utfall eller resonera om odds.

Match: {home} mot {away}, {league}, spelas {date}.

Sök efter nyheter från de senaste sju dagarna om skador, avstängningar,
sjukdomar, tränarbyten och besked om laguppställning.
{sources}

Svara med enbart JSON, utan förklaring och utan kodstaket:

{{"items": [
  {{"team": "{home} eller {away}",
    "text": "en mening på svenska",
    "source": "källans namn",
    "url": "fullständig länk",
    "date": "ÅÅÅÅ-MM-DD"}}
]}}

Regler:
- Högst {maxi} poster per lag.
- Varje post måste ha en fungerande länk till sidan där uppgiften står.
- Hittar du inget, svara {{"items": []}}. Hitta aldrig på.
- Skriv inget om form, tabelläge eller sannolikheter."""


# Verktygets namn har hetat olika saker i olika versioner av API:et.
# Vi provar tills ett fungerar och håller fast vid det.
TOOL_CANDIDATES = [os.environ.get("NEWS_TOOL"), "web_search", "web_search_preview"]
TOOL_CANDIDATES = [t for t in TOOL_CANDIDATES if t]
chosen_tool = None


def call_model(payload):
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # OpenAI förklarar alltid vad som är fel i svarskroppen. Utan den
        # ser man bara statuskoden, vilket inte hjälper någon.
        detail = ""
        try:
            body = json.load(exc)
            detail = ((body.get("error") or {}).get("message")) or json.dumps(body)
        except Exception:
            detail = "kunde inte läsa svaret"
        raise RuntimeError("HTTP %s: %s" % (exc.code, detail))


def ask(prompt):
    """Provar verktygsnamnen tills ett accepteras."""
    global chosen_tool

    names = [chosen_tool] if chosen_tool else list(TOOL_CANDIDATES)
    last = None

    for name in names:
        try:
            body = call_model({
                "model": MODEL,
                "input": prompt,
                "tools": [{"type": name}],
            })
            if not chosen_tool:
                chosen_tool = name
                print("Använder verktyget '%s'." % name)
            return body
        except RuntimeError as exc:
            last = exc
            if "HTTP 400" in str(exc):
                print("  Verktyget '%s' avvisades: %s" % (name, exc), file=sys.stderr)
                continue
            raise

    # Sista utvägen: kör utan sökverktyg, så vi åtminstone ser om
    # resten av anropet fungerar.
    print("  Inget sökverktyg accepterades, provar utan.", file=sys.stderr)
    try:
        return call_model({"model": MODEL, "input": prompt})
    except RuntimeError:
        raise last


def extract_text(body):
    """Plockar ut modellens text ur svaret, oavsett hur det är förpackat."""
    if isinstance(body.get("output_text"), str):
        return body["output_text"]

    chunks = []
    for item in body.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text") and part.get("text"):
                chunks.append(part["text"])
    return "\n".join(chunks)


def parse_items(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return []
    try:
        return (json.loads(text[start:end + 1]) or {}).get("items") or []
    except ValueError:
        return []


def link_works(url):
    """En länk som inte svarar är oftast en påhittad länk."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=LINK_TIMEOUT) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        # Vissa sajter nekar HEAD men finns. Prova en vanlig hämtning.
        if exc.code in (403, 405):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=LINK_TIMEOUT) as resp:
                    return 200 <= resp.status < 400
            except Exception:
                return False
        return False
    except Exception:
        return False


def load_json(path, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return fallback


def team_domains(name, per_team):
    """Domäner för ett lag. Tål både en sträng och en lista i filen."""
    value = per_team.get(name)
    if not value:
        # Tolerant matchning, så att Tottenham hittar Tottenham Hotspur.
        low = name.lower()
        for key, val in per_team.items():
            k = key.lower()
            if k == low or k.startswith(low) or low.startswith(k):
                value = val
                break
    if not value:
        return []
    return [d for d in (value if isinstance(value, list) else [value]) if d]


def read_events():
    payload = load_json("data.json", {})
    draw = payload.get("draw") or {}
    if isinstance(draw, dict) and "draws" in draw:
        draws = draw.get("draws") or []
        if not draws:
            return None, []
        draw = draws[0]
    events = [e for e in draw.get("drawEvents", []) if e.get("eventTypeId") != 2]
    return draw.get("drawNumber"), events


def main():
    if not KEY:
        print("OPENAI_API_KEY saknas, hoppar över.", file=sys.stderr)
        return

    draw_number, events = read_events()
    if not events:
        print("Ingen öppen omgång, hoppar över.")
        return

    previous = load_json(OUT, {})

    conf = load_json(SOURCES, {}) or {}
    # Äldre filer använde nyckeln betrodda.
    general = list(conf.get("allmanna") or conf.get("betrodda") or [])
    per_team = conf.get("lag") or {}

    trusted = set(general)
    for value in per_team.values():
        for domain in (value if isinstance(value, list) else [value]):
            if domain:
                trusted.add(domain)

    # Ny omgång betyder att gamla nyheter inte längre gäller.
    if str(previous.get("drawNumber")) != str(draw_number):
        previous = {"drawNumber": draw_number, "matches": {}}

    matches = dict(previous.get("matches") or {})
    calls = 0

    for event in events[:MAX_MATCHES]:
        match = event.get("match") or {}
        key = str(match.get("matchId") or event.get("eventNumber") or "")
        parts = match.get("participants") or []
        home = next((p.get("name") for p in parts if p.get("type") == "home"), "")
        away = next((p.get("name") for p in parts if p.get("type") == "away"), "")
        if not key or not home or not away:
            continue

        league = ((match.get("league") or {}).get("name")) or ""
        kickoff = (match.get("matchStart") or "")[:10]

        hints = []
        for name in (home, away):
            for domain in team_domains(name, per_team):
                hints.append(name + ": " + domain)

        source_text = "Sök i första hand på dessa webbplatser:\n"
        if hints:
            source_text += "\n".join("- " + h for h in hints) + "\n"
        if general:
            source_text += "- allmänna: " + ", ".join(general) + "\n"

        prompt = PROMPT.format(home=home, away=away, league=league,
                               date=kickoff, maxi=MAX_ITEMS,
                               sources=source_text)

        try:
            body = ask(prompt)
            calls += 1
        except Exception as exc:
            print("Anropet för %s misslyckades: %s" % (key, exc), file=sys.stderr)
            continue

        items = parse_items(extract_text(body))
        kept = []

        for item in items:
            url = (item.get("url") or "").strip()
            text = (item.get("text") or "").strip()
            if not url or not text:
                continue
            if not link_works(url):
                print("  Länk svarar inte, hoppas över: %s" % url)
                continue

            host = urllib.parse.urlparse(url).netloc.lower()
            host = host[4:] if host.startswith("www.") else host
            kept.append({
                "team": item.get("team") or "",
                "text": text,
                "source": item.get("source") or host,
                "url": url,
                "date": item.get("date") or "",
                "trusted": any(host == d or host.endswith("." + d) for d in trusted),
            })

        matches[key] = {
            "home": home, "away": away,
            "items": kept[:MAX_ITEMS * 2],
            "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        print("%s mot %s: %d av %d poster behölls." %
              (home, away, len(kept), len(items)))

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"drawNumber": draw_number,
                   "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "model": MODEL,
                   "matches": matches}, fh, ensure_ascii=False, indent=1)

    print("Klart. %d anrop till modellen." % calls)


if __name__ == "__main__":
    main()
