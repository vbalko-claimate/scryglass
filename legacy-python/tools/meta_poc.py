#!/usr/bin/env python3
"""
Proof-of-concept: Scrape MTGGoldfish Standard metagame data and generate
a meta_decks.json-compatible structure with signal_card weights,
archetype/speed estimation, and comparison against existing data.

Usage: uv run python tools/meta_poc.py
"""

import json
import re
import time
import sys
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://www.mtggoldfish.com"
META_URL = f"{BASE_URL}/metagame/standard/full"
TOP_N = 15
REQUEST_DELAY = 1.5  # seconds between requests (be respectful)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# Path to existing meta_decks.json for comparison
EXISTING_META = Path(__file__).resolve().parent.parent / "data" / "meta" / "meta_decks.json"

# ---------------------------------------------------------------------------
# Land detection
# ---------------------------------------------------------------------------
BASIC_LANDS = {
    "Plains", "Island", "Swamp", "Mountain", "Forest",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
    "Wastes",
}

# Exact known land names (not matched by suffix patterns)
KNOWN_LANDS = {
    # Fetchlands / special
    "Fabled Passage", "Evolving Wilds", "Field of Ruin", "Demolition Field",
    "Mirrex", "Multiversal Passage", "The Mycosynth Gardens", "Cavern of Souls",
    # Kamigawa channel lands
    "Otawara, Soaring City", "Eiganjo, Seat of the Empire",
    "Takenuma, Abandoned Mire", "Sokenzan, Crucible of Defiance",
    "Boseiju, Who Endures",
    # Restless creature-lands
    "Restless Reef", "Restless Vents", "Restless Fortress",
    "Restless Anchorage", "Restless Bivouac", "Restless Cottage",
    "Restless Prairie", "Restless Ridgeline", "Restless Vinestalk",
    # Standard dual lands / utility
    "Agna Qel'a", "Soulstone Sanctuary", "Meticulous Archive",
    "Cori Mountain Monastery", "Elegant Parlor", "Mistrise Village",
    "Ba Sing Se", "Starting Town", "Promising Vein",
    # Fastlands
    "Inspiring Vantage", "Botanical Sanctum", "Blooming Marsh",
    "Spirebluff Canal", "Concealed Courtyard",
    "Darkslick Shores", "Copperline Gorge", "Razorverge Thicket",
    "Seachrome Coast", "Blackcleave Cliffs",
    # Shocklands
    "Steam Vents", "Watery Grave", "Godless Shrine", "Blood Crypt",
    "Stomping Ground", "Temple Garden", "Hallowed Fountain",
    "Sacred Foundry", "Overgrown Tomb", "Breeding Pool",
    # Surveil lands
    "Undercity Sewers", "Shadowy Backstreet", "Lush Portico",
    "Commercial District", "Hedge Maze", "Thundering Falls",
    # Escape Tunnel (utility land-like, enters tapped)
    "Escape Tunnel",
}

# Suffixes that reliably indicate a land card
LAND_SUFFIXES = [
    "Verge", "Passage", "Triome", "Pathway", "Gate",
    "Grounds", "Heath", "Flats", "Marsh", "Mire", "Tarn",
    "Forge", "Pool", "Crypt", "Tomb", "Catacomb",
    "Harbor", "Hinterlands", "Grasslands", "Barrens",
    "Springs", "Falls", "Fountain", "Ruins",
]

# Color name → WUBRG mapping for HTML mana icons
MANA_ICON_MAP = {"w": "W", "u": "U", "b": "B", "r": "R", "g": "G"}

# Fallback: color mapping from deck name prefixes
COLOR_NAME_MAP = {
    "mono-white": ["W"], "mono-blue": ["U"], "mono-black": ["B"],
    "mono-red": ["R"], "mono-green": ["G"],
    "azorius": ["W", "U"], "orzhov": ["W", "B"], "boros": ["R", "W"],
    "selesnya": ["G", "W"], "dimir": ["U", "B"], "izzet": ["U", "R"],
    "rakdos": ["B", "R"], "golgari": ["B", "G"], "gruul": ["R", "G"],
    "simic": ["U", "G"],
    "esper": ["W", "U", "B"], "grixis": ["U", "B", "R"],
    "jund": ["B", "R", "G"], "naya": ["R", "G", "W"],
    "bant": ["G", "W", "U"], "abzan": ["W", "B", "G"],
    "jeskai": ["W", "U", "R"], "sultai": ["U", "B", "G"],
    "mardu": ["W", "B", "R"], "temur": ["U", "R", "G"],
    "4c": ["W", "U", "B", "R"], "5c": ["W", "U", "B", "R", "G"],
}


def fetch_url(url: str) -> str:
    """Fetch a URL and return its text content."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError) as e:
        print(f"  [ERROR] Failed to fetch {url}: {e}", file=sys.stderr)
        return ""


def is_land(card_name: str) -> bool:
    """Heuristic: is this card a land?"""
    if card_name in BASIC_LANDS or card_name in KNOWN_LANDS:
        return True
    name_lower = card_name.lower()
    for suffix in LAND_SUFFIXES:
        if suffix.lower() in name_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# Metagame page parsing — uses archetype-tile HTML structure
# ---------------------------------------------------------------------------
def parse_metagame_page(html: str) -> list[dict]:
    """
    Parse the MTGGoldfish metagame page.  Each deck lives inside a
    <div class='archetype-tile' id='…'> … </div> block.

    Returns list of {name, meta_pct, url, colors} dicts sorted by meta_pct desc.
    """
    # Split HTML into per-tile chunks
    tile_starts = [m.start() for m in re.finditer(r"<div class='archetype-tile'", html)]
    if not tile_starts:
        return []

    # Each tile ends where the next one begins (or at end of HTML)
    tiles = []
    for i, start in enumerate(tile_starts):
        end = tile_starts[i + 1] if i + 1 < len(tile_starts) else len(html)
        tiles.append(html[start:end])

    decks: list[dict] = []
    for tile in tiles:
        # --- Deck name + URL from the "online" variant link ---
        # <span class='deck-price-online'>
        #   <a href="/archetype/standard-izzet-lessons-woe#online">Izzet Lessons</a>
        name_match = re.search(
            r"deck-price-online[^>]*>\s*<a\s+href=\"(/archetype/[^\"]+)\"[^>]*>([^<]+)</a>",
            tile,
        )
        if not name_match:
            continue
        url_path = name_match.group(1)
        deck_name = name_match.group(2).strip()

        # --- Meta percentage ---
        pct_match = re.search(
            r"metagame-percentage.*?statistic-value[^>]*>\s*([\d.]+)%",
            tile,
            re.DOTALL,
        )
        meta_pct = float(pct_match.group(1)) if pct_match else 0.0

        # --- Colors from mana icons: <i class='ms ms-u ms-cost ms-shadow'> ---
        color_icons = re.findall(r"ms ms-([wubrg]) ms-cost", tile)
        colors = []
        seen = set()
        for c in color_icons:
            mapped = MANA_ICON_MAP.get(c, "")
            if mapped and mapped not in seen:
                colors.append(mapped)
                seen.add(mapped)

        # Fallback to name-based colors
        if not colors:
            name_lower = deck_name.lower()
            for prefix, cols in COLOR_NAME_MAP.items():
                if prefix in name_lower:
                    colors = cols
                    break

        decks.append({
            "name": deck_name,
            "meta_pct": meta_pct,
            "url": f"{BASE_URL}{url_path}",
            "colors": colors,
        })

    # Sort by meta percentage descending (should already be in order)
    decks.sort(key=lambda d: d["meta_pct"], reverse=True)
    return decks


# ---------------------------------------------------------------------------
# Decklist parsing from archetype pages
# ---------------------------------------------------------------------------
def parse_decklist_from_js(html: str) -> list[tuple[int, str]]:
    """
    Extract decklist from initializeDeckComponents() JS call.
    The third argument is a URL-encoded decklist string.
    Returns list of (count, card_name) tuples (mainboard only).
    """
    js_match = re.search(
        r'initializeDeckComponents\([^,]+,\s*[^,]+,\s*"([^"]+)"', html
    )
    if not js_match:
        return []

    decoded = unquote(js_match.group(1))
    cards = []
    in_sideboard = False

    for line in decoded.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.lower() == "sideboard":
            in_sideboard = True
            continue
        if in_sideboard:
            continue
        m = re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            cards.append((int(m.group(1)), m.group(2).strip()))

    return cards


def parse_decklist_from_download(html: str) -> list[tuple[int, str]]:
    """Fallback: use the /deck/download/ link on the archetype page."""
    dl_match = re.search(r'href="(/deck/download/\d+)"', html)
    if not dl_match:
        return []
    dl_url = f"{BASE_URL}{dl_match.group(1)}"
    time.sleep(REQUEST_DELAY)
    text = fetch_url(dl_url)
    if not text:
        return []

    cards = []
    in_sideboard = False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("sideboard"):
            in_sideboard = True
            continue
        if in_sideboard:
            continue
        m = re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            cards.append((int(m.group(1)), m.group(2).strip()))
    return cards


# ---------------------------------------------------------------------------
# Signal card weight computation
# ---------------------------------------------------------------------------
def compute_signal_weights(cards: list[tuple[int, str]]) -> dict[str, float]:
    """
    4-of nonland cards get highest weight, scaling down for fewer copies.
    Normalised so weights sum to ~1.0, capped at top 8 cards.
    """
    nonland = [(count, name) for count, name in cards if not is_land(name)]
    if not nonland:
        return {}

    # Weight by copies: 4-of → 1.0, 3 → 0.70, 2 → 0.40, 1 → 0.15
    copy_weight = {4: 1.0, 3: 0.70, 2: 0.40, 1: 0.15}

    raw = {}
    for count, name in nonland:
        raw[name] = copy_weight.get(min(count, 4), 0.15)

    total = sum(raw.values())
    if total == 0:
        return {}

    # Normalise, sort descending, take top 8, drop tiny weights
    sorted_items = sorted(raw.items(), key=lambda kv: -kv[1])
    result = {}
    for name, w in sorted_items[:8]:
        nw = round(w / total, 2)
        if nw >= 0.03:
            result[name] = nw

    return result


# ---------------------------------------------------------------------------
# Archetype / speed estimation
# ---------------------------------------------------------------------------
def estimate_archetype(cards: list[tuple[int, str]], deck_name: str) -> str:
    """Estimate aggro / midrange / control / combo from deck name + card mix."""
    n = deck_name.lower()

    # Strong name-based hints first
    if any(kw in n for kw in ["aggro", "burn", "prowess", "dragons", "leyline aggro"]):
        return "aggro"
    if any(kw in n for kw in ["control"]):
        return "control"
    if any(kw in n for kw in ["combo", "reanimator", "omniscience"]):
        return "combo"
    if any(kw in n for kw in ["lessons", "self-bounce"]):
        return "combo"
    if any(kw in n for kw in ["tempo", "flash", "midrange", "monument", "discard"]):
        return "midrange"
    if any(kw in n for kw in ["excruciator"]):
        return "combo"
    if any(kw in n for kw in ["landfall", "elementals", "spellementals"]):
        return "midrange"
    if any(kw in n for kw in ["tokens"]):
        return "aggro"
    if any(kw in n for kw in ["ouroboroid"]):
        return "combo"
    if any(kw in n for kw in ["faeries"]):
        return "midrange"

    # Fallback: high nonland count suggests creature-heavy = aggro
    nonland = [(c, nm) for c, nm in cards if not is_land(nm)]
    total = sum(c for c, _ in nonland)
    if total == 0:
        return "midrange"

    # Count 4-ofs — lots of 4-ofs usually means aggro (consistency-focused)
    four_ofs = sum(c for c, _ in nonland if c == 4)
    if four_ofs / total > 0.7:
        return "aggro"

    return "midrange"


def estimate_speed(archetype: str, deck_name: str) -> tuple[str, int]:
    """Return (speed, typical_kill_turn)."""
    n = deck_name.lower()

    if archetype == "aggro":
        if any(kw in n for kw in ["mono-red", "burn", "prowess", "leyline"]):
            return "very_fast", 5
        return "fast", 6

    if archetype == "combo":
        if "reanimator" in n:
            return "medium", 7
        if "omniscience" in n:
            return "slow", 8
        if "ouroboroid" in n:
            return "very_fast", 5
        if "excruciator" in n:
            return "medium", 7
        return "medium", 6

    if archetype == "control":
        return "slow", 9

    # midrange
    if any(kw in n for kw in ["tempo", "flash"]):
        return "medium", 7
    if "landfall" in n:
        return "medium", 6
    return "medium", 7


# ---------------------------------------------------------------------------
# Build JSON-format deck entry
# ---------------------------------------------------------------------------
def build_meta_deck(deck_info: dict, cards: list[tuple[int, str]]) -> dict:
    """Build a meta_decks.json-format entry."""
    name = deck_info["name"]
    meta_pct = deck_info["meta_pct"]
    colors = deck_info.get("colors", [])

    signal_cards = compute_signal_weights(cards)
    archetype = estimate_archetype(cards, name)
    speed, kill_turn = estimate_speed(archetype, name)

    return {
        "name": name,
        "archetype": archetype,
        "colors": colors,
        "signal_cards": signal_cards,
        "key_threats": [],  # requires manual curation
        "speed": speed,
        "typical_kill_turn": kill_turn,
        "hidden_reach": 0,  # requires manual assessment
        "description": f"Meta share: {meta_pct}%. Auto-generated — needs manual review.",
    }


# ---------------------------------------------------------------------------
# Comparison with existing data
# ---------------------------------------------------------------------------
def load_existing_meta() -> dict:
    if not EXISTING_META.exists():
        return {"meta_decks": []}
    with open(EXISTING_META) as f:
        return json.load(f)


def _normalise_name(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def compare_meta(new_decks: list[dict], existing_data: dict) -> None:
    """Print comparison between scraped data and existing meta_decks.json."""
    existing_list = existing_data.get("meta_decks", [])
    existing_norm = {_normalise_name(d["name"]): d for d in existing_list}
    new_norm = {_normalise_name(d["name"]): d for d in new_decks}

    print("\n" + "=" * 70)
    print("COMPARISON WITH EXISTING meta_decks.json")
    print("=" * 70)

    # --- New decks ---
    truly_new = []
    for nn, deck in new_norm.items():
        if nn in existing_norm:
            continue
        # Partial match?
        if any(nn in en or en in nn for en in existing_norm):
            continue
        truly_new.append(deck["name"])

    if truly_new:
        print(f"\n  NEW DECKS (not in existing data) [{len(truly_new)}]:")
        for name in truly_new:
            d = next(d for d in new_decks if d["name"] == name)
            pct = d.get("description", "").split("%")[0].split(": ")[-1] if d.get("description") else "?"
            print(f"    + {name} ({d['archetype']}, ~{pct}%)")
    else:
        print("\n  No completely new archetypes found.")

    # --- Missing decks ---
    missing = []
    for en, deck in existing_norm.items():
        if en in new_norm:
            continue
        if any(en in nn or nn in en for nn in new_norm):
            continue
        missing.append(deck["name"])

    if missing:
        print(f"\n  MISSING FROM CURRENT META (in existing but not scraped top {TOP_N}) [{len(missing)}]:")
        for name in missing:
            print(f"    - {name}")
    else:
        print("\n  No decks dropped from meta.")

    # --- Signal card diffs for matching decks ---
    print("\n  SIGNAL CARD CHANGES:")
    matches = 0
    for nn, new_deck in new_norm.items():
        existing_deck = existing_norm.get(nn)
        if not existing_deck:
            # partial match
            for en, ed in existing_norm.items():
                if nn in en or en in nn:
                    existing_deck = ed
                    break
        if not existing_deck:
            continue

        matches += 1
        old_sigs = set(existing_deck.get("signal_cards", {}).keys())
        new_sigs = set(new_deck.get("signal_cards", {}).keys())
        added = new_sigs - old_sigs
        removed = old_sigs - new_sigs

        if added or removed:
            print(f"\n    {new_deck['name']} vs {existing_deck['name']}:")
            for card in sorted(added):
                print(f"      + {card} (w={new_deck['signal_cards'][card]})")
            for card in sorted(removed):
                print(f"      - {card}")

    if matches == 0:
        print("    (no matching deck names for comparison)")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print("MTGGoldfish Standard Metagame Scraper (Proof of Concept)")
    print("=" * 70)

    # Step 1: Fetch metagame overview
    print(f"\nFetching metagame page: {META_URL}")
    meta_html = fetch_url(META_URL)
    if not meta_html:
        print("ERROR: Could not fetch metagame page.", file=sys.stderr)
        sys.exit(1)

    decks = parse_metagame_page(meta_html)
    print(f"Found {len(decks)} deck archetypes.")

    if not decks:
        print("ERROR: No decks parsed. Page format may have changed.", file=sys.stderr)
        sys.exit(1)

    # Show all decks
    print(f"\nAll decks found:")
    for i, d in enumerate(decks, 1):
        colors = "".join(d["colors"]) or "?"
        print(f"  {i:2d}. {d['name']:40s} {d['meta_pct']:5.1f}%  [{colors}]")

    # Step 2: Fetch top N archetype pages for decklists
    top_decks = decks[:TOP_N]
    print(f"\nFetching decklists for top {TOP_N} decks...\n")

    results = []
    for i, deck_info in enumerate(top_decks, 1):
        print(f"  [{i:2d}/{TOP_N}] {deck_info['name']} ({deck_info['meta_pct']}%)")
        print(f"         {deck_info['url']}")

        if i > 1:  # no delay before first request
            time.sleep(REQUEST_DELAY)
        arch_html = fetch_url(deck_info["url"])

        if not arch_html:
            print("         [SKIP] fetch failed")
            continue

        # Parse decklist: JS blob first, then download fallback
        cards = parse_decklist_from_js(arch_html)
        src = "JS"
        if not cards:
            cards = parse_decklist_from_download(arch_html)
            src = "download"
        if not cards:
            print("         [SKIP] could not parse decklist")
            continue

        nonland = [(c, n) for c, n in cards if not is_land(n)]
        land = [(c, n) for c, n in cards if is_land(n)]
        total = sum(c for c, _ in cards)

        print(f"         Parsed ({src}): {total} cards "
              f"({sum(c for c,_ in nonland)} nonland, {sum(c for c,_ in land)} land)")

        # Print nonland cards
        for count, name in sorted(nonland, key=lambda x: (-x[0], x[1])):
            print(f"           {count}x {name}")

        meta_deck = build_meta_deck(deck_info, cards)
        results.append(meta_deck)

        sig_preview = list(meta_deck["signal_cards"].keys())[:5]
        print(f"         => {meta_deck['archetype']}, {meta_deck['speed']}, "
              f"kill T{meta_deck['typical_kill_turn']}")
        print(f"         => signals: {sig_preview}")
        print()

    # Step 3: Output JSON
    output = {"meta_decks": results}
    print("=" * 70)
    print("GENERATED meta_decks.json STRUCTURE")
    print("=" * 70)
    print(json.dumps(output, indent=2, ensure_ascii=False))

    # Step 4: Compare with existing
    existing = load_existing_meta()
    compare_meta(results, existing)

    print(f"Done. Generated {len(results)} deck entries from top {TOP_N} archetypes.")
    print("NOTE: key_threats, hidden_reach, and descriptions need manual curation.")


if __name__ == "__main__":
    main()
