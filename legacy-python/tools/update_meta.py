#!/usr/bin/env python3
"""Update meta_decks.json from MTGGoldfish + observed match data + optional LLM.

Usage:
    uv run python tools/update_meta.py              # scrape + merge + LLM enrich
    uv run python tools/update_meta.py --no-llm     # scrape + merge, skip LLM
    uv run python tools/update_meta.py --dry-run    # preview changes, don't write

Sources (in priority order):
  1. MTGGoldfish Standard metagame (public, no auth)
  2. Existing meta_decks.json (preserve manual key_threats, descriptions)
  3. Observed opponent decks from match history DB
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = "https://www.mtggoldfish.com"
META_URL = f"{BASE_URL}/metagame/standard/full"
TOP_N = 20  # fetch decklists for top N
REQUEST_DELAY = 1.5
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

META_DECKS_PATH = Path(__file__).parent.parent / "data" / "meta" / "meta_decks.json"
from advisor.database import DB_PATH

# ---------------------------------------------------------------------------
# Land detection (reused from PoC)
# ---------------------------------------------------------------------------
BASIC_LANDS = {
    "Plains", "Island", "Swamp", "Mountain", "Forest",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest", "Wastes",
}

KNOWN_LANDS = {
    "Fabled Passage", "Evolving Wilds", "Field of Ruin", "Demolition Field",
    "Mirrex", "Multiversal Passage", "The Mycosynth Gardens", "Cavern of Souls",
    "Otawara, Soaring City", "Eiganjo, Seat of the Empire",
    "Takenuma, Abandoned Mire", "Sokenzan, Crucible of Defiance",
    "Boseiju, Who Endures",
    "Restless Reef", "Restless Vents", "Restless Fortress",
    "Restless Anchorage", "Restless Bivouac", "Restless Cottage",
    "Restless Prairie", "Restless Ridgeline", "Restless Vinestalk",
    "Agna Qel'a", "Soulstone Sanctuary", "Meticulous Archive",
    "Cori Mountain Monastery", "Elegant Parlor", "Mistrise Village",
    "Ba Sing Se", "Starting Town", "Promising Vein",
    "Inspiring Vantage", "Botanical Sanctum", "Blooming Marsh",
    "Spirebluff Canal", "Concealed Courtyard",
    "Darkslick Shores", "Copperline Gorge", "Razorverge Thicket",
    "Seachrome Coast", "Blackcleave Cliffs",
    "Steam Vents", "Watery Grave", "Godless Shrine", "Blood Crypt",
    "Stomping Ground", "Temple Garden", "Hallowed Fountain",
    "Sacred Foundry", "Overgrown Tomb", "Breeding Pool",
    "Undercity Sewers", "Shadowy Backstreet", "Lush Portico",
    "Commercial District", "Hedge Maze", "Thundering Falls",
    "Escape Tunnel",
}

LAND_SUFFIXES = [
    "Verge", "Passage", "Triome", "Pathway", "Gate",
    "Grounds", "Heath", "Flats", "Marsh", "Mire", "Tarn",
    "Forge", "Pool", "Crypt", "Tomb", "Catacomb",
    "Harbor", "Hinterlands", "Grasslands", "Barrens",
    "Springs", "Falls", "Fountain", "Ruins",
]

MANA_ICON_MAP = {"w": "W", "u": "U", "b": "B", "r": "R", "g": "G"}

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
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (URLError, HTTPError) as e:
        print(f"  [ERROR] {url}: {e}", file=sys.stderr)
        return ""


def is_land(card_name: str) -> bool:
    if card_name in BASIC_LANDS or card_name in KNOWN_LANDS:
        return True
    name_lower = card_name.lower()
    for suffix in LAND_SUFFIXES:
        if suffix.lower() in name_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# MTGGoldfish parsing
# ---------------------------------------------------------------------------
def parse_metagame_page(html: str) -> list[dict]:
    tile_starts = [m.start() for m in re.finditer(r"<div class='archetype-tile'", html)]
    if not tile_starts:
        return []

    tiles = []
    for i, start in enumerate(tile_starts):
        end = tile_starts[i + 1] if i + 1 < len(tile_starts) else len(html)
        tiles.append(html[start:end])

    decks = []
    for tile in tiles:
        name_match = re.search(
            r"deck-price-online[^>]*>\s*<a\s+href=\"(/archetype/[^\"]+)\"[^>]*>([^<]+)</a>",
            tile)
        if not name_match:
            continue
        url_path = name_match.group(1)
        deck_name = name_match.group(2).strip()

        pct_match = re.search(
            r"metagame-percentage.*?statistic-value[^>]*>\s*([\d.]+)%",
            tile, re.DOTALL)
        meta_pct = float(pct_match.group(1)) if pct_match else 0.0

        color_icons = re.findall(r"ms ms-([wubrg]) ms-cost", tile)
        colors = []
        seen = set()
        for c in color_icons:
            mapped = MANA_ICON_MAP.get(c, "")
            if mapped and mapped not in seen:
                colors.append(mapped)
                seen.add(mapped)
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

    decks.sort(key=lambda d: d["meta_pct"], reverse=True)
    return decks


def parse_decklist_from_js(html: str) -> list[tuple[int, str]]:
    js_match = re.search(
        r'initializeDeckComponents\([^,]+,\s*[^,]+,\s*"([^"]+)"', html)
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


def compute_signal_weights(cards: list[tuple[int, str]]) -> dict[str, float]:
    nonland = [(count, name) for count, name in cards if not is_land(name)]
    if not nonland:
        return {}

    copy_weight = {4: 1.0, 3: 0.70, 2: 0.40, 1: 0.15}
    raw = {}
    for count, name in nonland:
        raw[name] = copy_weight.get(min(count, 4), 0.15)

    total = sum(raw.values())
    if total == 0:
        return {}

    sorted_items = sorted(raw.items(), key=lambda kv: -kv[1])
    result = {}
    for name, w in sorted_items[:8]:
        nw = round(w / total, 2)
        if nw >= 0.03:
            result[name] = nw
    return result


def estimate_archetype(cards: list[tuple[int, str]], deck_name: str) -> str:
    n = deck_name.lower()
    if any(kw in n for kw in ["aggro", "burn", "prowess", "dragons", "leyline aggro", "tokens"]):
        return "aggro"
    if any(kw in n for kw in ["control"]):
        return "control"
    if any(kw in n for kw in ["combo", "reanimator", "omniscience", "lessons",
                               "self-bounce", "excruciator", "ouroboroid"]):
        return "combo"
    if any(kw in n for kw in ["tempo", "flash", "midrange", "monument", "discard"]):
        return "midrange"
    if any(kw in n for kw in ["landfall", "elementals", "spellementals", "faeries"]):
        return "midrange"
    return "midrange"


def estimate_speed(archetype: str, deck_name: str) -> tuple[str, int]:
    n = deck_name.lower()
    if archetype == "aggro":
        if any(kw in n for kw in ["mono-red", "burn", "prowess", "leyline"]):
            return "very_fast", 5
        return "fast", 6
    if archetype == "combo":
        if "ouroboroid" in n:
            return "very_fast", 5
        if "reanimator" in n:
            return "medium", 7
        if "omniscience" in n:
            return "slow", 8
        if "excruciator" in n:
            return "medium", 7
        return "medium", 6
    if archetype == "control":
        return "slow", 9
    if any(kw in n for kw in ["tempo", "flash"]):
        return "medium", 7
    if "landfall" in n:
        return "medium", 6
    return "medium", 7


# ---------------------------------------------------------------------------
# Observed decks from match DB
# ---------------------------------------------------------------------------
def load_observed_decks() -> list[dict]:
    """Load opponent deck profiles from our match history."""
    if not DB_PATH.exists():
        return []
    try:
        from advisor.database import get_observed_opp_decks
        return get_observed_opp_decks(min_cards=3)
    except Exception as e:
        print(f"  [WARN] Could not load observed decks: {e}")
        return []


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_existing(name: str, existing: dict[str, dict]) -> dict | None:
    """Find matching deck in existing data by normalized name."""
    nn = _norm(name)
    if nn in existing:
        return existing[nn]
    # Partial match
    for en, ed in existing.items():
        if nn in en or en in nn:
            return ed
    return None


def merge_meta_decks(
    scraped: list[dict],
    observed: list[dict],
    existing_list: list[dict],
) -> list[dict]:
    """Merge scraped + observed + existing into final meta_decks list.

    Priority:
    - Scraped data overwrites signal_cards, colors, archetype, speed, kill_turn
    - Existing key_threats, description, hidden_reach are PRESERVED
    - Observed decks added at the end if not already present
    - needs_review flag set for decks missing key_threats or description
    """
    existing_by_norm = {_norm(d["name"]): d for d in existing_list}
    result = []
    seen_norms = set()

    # 1. Scraped decks (highest priority)
    for deck in scraped:
        nn = _norm(deck["name"])
        seen_norms.add(nn)

        old = _find_existing(deck["name"], existing_by_norm)
        if old:
            # Preserve manually curated fields
            deck["key_threats"] = old.get("key_threats", [])
            deck["hidden_reach"] = old.get("hidden_reach", 0)
            old_desc = old.get("description", "")
            if old_desc and "Auto-generated" not in old_desc:
                deck["description"] = old_desc
            # Keep old archetype/speed if manually set and different
            # (but prefer scraped if name-based heuristic is confident)

        # Set needs_review flag
        has_threats = bool(deck.get("key_threats"))
        has_desc = bool(deck.get("description")) and "Auto-generated" not in deck.get("description", "")
        deck["needs_review"] = not (has_threats and has_desc)

        result.append(deck)

    # 2. Existing decks NOT in scraped (dropped from meta but still useful)
    for old_deck in existing_list:
        nn = _norm(old_deck["name"])
        if nn in seen_norms:
            continue
        # Check partial match
        if any(nn in sn or sn in nn for sn in seen_norms):
            continue
        # Keep it but mark as dropped
        old_deck["meta_pct"] = 0
        old_deck["needs_review"] = old_deck.get("needs_review", False)
        result.append(old_deck)
        seen_norms.add(nn)

    # 3. Observed decks from our match history
    for obs in observed:
        nn = _norm(obs["name"])
        if nn in seen_norms:
            continue
        if any(nn in sn or sn in nn for sn in seen_norms):
            continue
        deck = {
            "name": obs["name"],
            "archetype": obs.get("archetype", "unknown"),
            "colors": obs.get("colors", []),
            "signal_cards": obs.get("signal_cards", {}),
            "key_threats": [],
            "speed": "medium",
            "typical_kill_turn": 7,
            "hidden_reach": 0,
            "description": f"Observed in {obs.get('matches', '?')} matches.",
            "source": "observed",
            "needs_review": True,
        }
        result.append(deck)
        seen_norms.add(nn)

    return result


# ---------------------------------------------------------------------------
# LLM enrichment
# ---------------------------------------------------------------------------
META_ENRICH_PROMPT = """You are an expert MTG Arena Standard player. Analyze this deck archetype and provide key threat assessments.

Deck: {name}
Archetype: {archetype} ({speed}, typical kill turn {kill_turn})
Colors: {colors}
Key cards: {signal_cards}

Respond in EXACTLY this format:

DESCRIPTION: 1-2 sentence description of the deck's game plan and what makes it dangerous.

Then for the top 3-4 most dangerous cards that opponents MUST deal with:

THREAT: card name
DANGER: high | medium | low
REASON: max 15 words why this card is dangerous
PRIORITY: 1-4 (1 = remove first)
MUST_ANSWER: true | false

Only include cards from the key cards list above."""


def _parse_enrich_response(text: str) -> tuple[str, list[dict]]:
    """Parse LLM enrichment response into (description, key_threats)."""
    description = ""
    threats = []
    current: dict = {}

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith("DESCRIPTION:"):
            description = line[12:].strip()
        elif upper.startswith("THREAT:"):
            if current.get("card"):
                threats.append(current)
            current = {"card": line[7:].strip()}
        elif upper.startswith("DANGER:"):
            current["danger"] = line[7:].strip().lower()
        elif upper.startswith("REASON:"):
            current["reason"] = line[7:].strip()
        elif upper.startswith("PRIORITY:"):
            try:
                current["removal_priority"] = int(line[9:].strip()[0])
            except (ValueError, IndexError):
                current["removal_priority"] = 3
        elif upper.startswith("MUST_ANSWER:"):
            val = line[12:].strip().lower()
            if val == "true":
                current["must_answer"] = True

    if current.get("card"):
        threats.append(current)

    return description, threats


async def enrich_with_llm(decks: list[dict]) -> int:
    """Try to enrich decks missing key_threats/description via LLM.

    Returns count of decks enriched.
    """
    # Import LLM backend
    try:
        from advisor.llm_advisor import get_backend, _call_claude_cli, _call_ollama, _call_anthropic_api
    except ImportError:
        print("  [WARN] Could not import LLM advisor — skipping enrichment")
        return 0

    import os
    backend = get_backend()
    if backend == "none":
        print("  [INFO] No LLM backend available — skipping enrichment")
        return 0

    # For batch enrichment, prefer API over CLI (no cold start per call)
    if backend == "claude_cli" and os.environ.get("ANTHROPIC_API_KEY"):
        backend = "anthropic_api"
        print(f"\n  LLM backend: {backend} (preferred over CLI for batch)")
    else:
        print(f"\n  LLM backend: {backend}")

    call_fn = {
        "claude_cli": _call_claude_cli,
        "ollama": _call_ollama,
        "anthropic_api": _call_anthropic_api,
    }.get(backend)

    if not call_fn:
        print(f"  [WARN] Unknown backend '{backend}' — skipping")
        return 0

    to_enrich = [d for d in decks if d.get("needs_review") and not d.get("key_threats")]
    if not to_enrich:
        print("  All decks already have key_threats — nothing to enrich")
        return 0

    print(f"  Enriching {len(to_enrich)} decks...")
    enriched = 0

    for deck in to_enrich:
        signals = ", ".join(deck.get("signal_cards", {}).keys())
        prompt = META_ENRICH_PROMPT.format(
            name=deck["name"],
            archetype=deck.get("archetype", "unknown"),
            speed=deck.get("speed", "medium"),
            kill_turn=deck.get("typical_kill_turn", 7),
            colors="/".join(deck.get("colors", [])),
            signal_cards=signals,
        )

        try:
            text = await call_fn(prompt)
            if not text or len(text) < 20:
                print(f"    {deck['name']}: empty LLM response, skipping")
                continue

            description, threats = _parse_enrich_response(text)

            if description:
                # Append meta share info
                meta_pct = deck.get("meta_pct", 0)
                if meta_pct:
                    description = f"{description} Meta share: {meta_pct}%."
                deck["description"] = description

            if threats:
                deck["key_threats"] = threats

            # Update needs_review
            if description and threats:
                deck["needs_review"] = False
                enriched += 1
                print(f"    {deck['name']}: +{len(threats)} threats")
            else:
                print(f"    {deck['name']}: partial (desc={'yes' if description else 'no'}, "
                      f"threats={len(threats)})")

        except Exception as e:
            print(f"    {deck['name']}: LLM error — {e}")
            continue

    return enriched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    dry_run = "--dry-run" in sys.argv
    no_llm = "--no-llm" in sys.argv

    print("=" * 70)
    print("Meta Decks Updater")
    print(f"  Source: MTGGoldfish Standard + match history + LLM")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}"
          f"{' (no LLM)' if no_llm else ''}")
    print("=" * 70)

    # Step 1: Load existing
    existing_list = []
    if META_DECKS_PATH.exists():
        existing_data = json.loads(META_DECKS_PATH.read_text())
        existing_list = existing_data.get("meta_decks", [])
    print(f"\nExisting meta decks: {len(existing_list)}")

    # Step 2: Scrape MTGGoldfish
    print(f"\nFetching metagame: {META_URL}")
    meta_html = fetch_url(META_URL)
    if not meta_html:
        print("ERROR: Could not fetch metagame page.", file=sys.stderr)
        sys.exit(1)

    all_decks = parse_metagame_page(meta_html)
    print(f"Found {len(all_decks)} archetypes on MTGGoldfish")

    top_decks = all_decks[:TOP_N]
    print(f"\nFetching decklists for top {TOP_N}...")

    scraped = []
    for i, deck_info in enumerate(top_decks, 1):
        if i > 1:
            time.sleep(REQUEST_DELAY)

        arch_html = fetch_url(deck_info["url"])
        if not arch_html:
            print(f"  [{i:2d}] {deck_info['name']} — FAILED")
            continue

        cards = parse_decklist_from_js(arch_html)
        if not cards:
            print(f"  [{i:2d}] {deck_info['name']} — no decklist")
            continue

        signal_cards = compute_signal_weights(cards)
        archetype = estimate_archetype(cards, deck_info["name"])
        speed, kill_turn = estimate_speed(archetype, deck_info["name"])

        entry = {
            "name": deck_info["name"],
            "archetype": archetype,
            "colors": deck_info["colors"],
            "signal_cards": signal_cards,
            "key_threats": [],
            "speed": speed,
            "typical_kill_turn": kill_turn,
            "hidden_reach": 0,
            "meta_pct": deck_info["meta_pct"],
            "description": f"Meta share: {deck_info['meta_pct']}%. Auto-generated — needs manual review.",
        }
        scraped.append(entry)
        print(f"  [{i:2d}] {deck_info['name']:35s} {deck_info['meta_pct']:5.1f}%  "
              f"{archetype:10s} {speed}")

    print(f"\nScraped {len(scraped)} decks with decklists")

    # Step 3: Load observed decks
    observed = load_observed_decks()
    if observed:
        print(f"Observed opponent decks from DB: {len(observed)}")
    else:
        print("No observed opponent decks in DB")

    # Step 4: Merge
    merged = merge_meta_decks(scraped, observed, existing_list)
    needs_review = sum(1 for d in merged if d.get("needs_review"))
    print(f"\nMerged total: {len(merged)} decks ({needs_review} need review)")

    # Step 5: LLM enrichment
    if not no_llm and needs_review > 0:
        print("\nAttempting LLM enrichment...")
        enriched = asyncio.run(enrich_with_llm(merged))
        remaining = sum(1 for d in merged if d.get("needs_review"))
        print(f"  Enriched: {enriched}, still need review: {remaining}")
    elif no_llm:
        print("\nSkipping LLM enrichment (--no-llm)")

    # Step 6: Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    from_scraped = [d for d in merged if d.get("meta_pct", 0) > 0 and d.get("source") != "observed"]
    from_existing = [d for d in merged if d.get("meta_pct", 0) == 0 and d.get("source") != "observed"]
    from_observed = [d for d in merged if d.get("source") == "observed"]
    review_needed = [d for d in merged if d.get("needs_review")]

    print(f"  From MTGGoldfish:  {len(from_scraped)}")
    print(f"  Kept from old:     {len(from_existing)}")
    print(f"  From match history: {len(from_observed)}")
    print(f"  Need review:       {len(review_needed)}")

    if review_needed:
        print("\n  Decks needing review:")
        for d in review_needed:
            missing = []
            if not d.get("key_threats"):
                missing.append("key_threats")
            if not d.get("description") or "Auto-generated" in d.get("description", ""):
                missing.append("description")
            print(f"    - {d['name']} (missing: {', '.join(missing)})")

    # Step 7: Write
    if dry_run:
        print("\n  DRY RUN — not writing to disk")
        print(f"\n  Would write to: {META_DECKS_PATH}")
    else:
        # Clean up internal fields before saving
        output_decks = []
        for d in merged:
            clean = {
                "name": d["name"],
                "archetype": d.get("archetype", "unknown"),
                "colors": d.get("colors", []),
                "signal_cards": d.get("signal_cards", {}),
                "key_threats": d.get("key_threats", []),
                "speed": d.get("speed", "medium"),
                "typical_kill_turn": d.get("typical_kill_turn", 7),
                "hidden_reach": d.get("hidden_reach", 0),
                "description": d.get("description", ""),
            }
            if d.get("needs_review"):
                clean["needs_review"] = True
            if d.get("source") == "observed":
                clean["source"] = "observed"
            if d.get("meta_pct", 0) > 0:
                clean["meta_pct"] = d["meta_pct"]
            output_decks.append(clean)

        output = {"meta_decks": output_decks}

        # Backup existing
        if META_DECKS_PATH.exists():
            backup = META_DECKS_PATH.with_suffix(".json.bak")
            META_DECKS_PATH.rename(backup)
            print(f"\n  Backup: {backup.name}")

        META_DECKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        META_DECKS_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False))
        print(f"  Written: {META_DECKS_PATH} ({len(output_decks)} decks)")

    print("\nDone.")


if __name__ == "__main__":
    main()
