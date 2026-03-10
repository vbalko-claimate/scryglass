"""Two-step LLM pipeline to generate deck strategy rules.

Step 1 — RESEARCH: Deep strategy analysis with web search.
  Produces a comprehensive guide: archetype, synergies, mulligan,
  matchups, meta positioning, sideboard tips.

Step 2 — COMPILE: Map the guide + deck list to JSON rules.
  Uses the rule contract to produce machine-readable strategy plugin.

Usage:
    cd ~/MTG/advisor

    # Full pipeline (both steps):
    uv run python -m advisor.compile_rules --deck ~/MTG/mono_white_lifegain_meta.txt

    # Step 1 only (research):
    uv run python -m advisor.compile_rules --deck DECK.txt --step research

    # Step 2 only (compile from existing guide):
    uv run python -m advisor.compile_rules --deck DECK.txt --step compile

    # List existing strategies:
    uv run python -m advisor.compile_rules --list
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

RULES_DIR = Path(__file__).parent.parent / "data" / "strategies"
CONTRACT_PATH = RULES_DIR / "rule_contract.md"


# ─── Step 1: Research Prompt ─────────────────────────────────────

def _build_research_prompt(deck_path: str, deck_name: str) -> str:
    return "\n".join([
        "You are an expert MTG Arena competitive coach and deck analyst.",
        f"Your task: produce a COMPREHENSIVE STRATEGY GUIDE for the deck '{deck_name}'.",
        "",
        f"First, read the deck list from: {deck_path}",
        "",
        "Then USE YOUR WEB SEARCH TOOLS to look up:",
        "1. Current MTG Arena Standard meta: top 5-8 decks, their key cards and speeds",
        "2. Strategy guides for this archetype: synergies, mulligan, play patterns",
        "Do 2-3 focused web searches, then write the guide using your MTG knowledge + search results.",
        "",
        "=== GUIDE STRUCTURE (follow exactly) ===",
        "",
        f"# Strategy Guide: {deck_name}",
        "",
        "## 1. Deck Overview",
        "- Archetype (aggro/midrange/control/combo) and playstyle",
        "- Win condition and game plan",
        "- Speed (what turn does this deck want to win?)",
        "- Strengths and weaknesses",
        "",
        "## 2. Key Cards & Roles",
        "For each notable card in the deck:",
        "- Role (threat, enabler, payoff, removal, utility)",
        "- When to cast it (sequencing priority)",
        "- Synergies with other cards in the deck",
        "- Protection priority (how important is it to keep alive?)",
        "",
        "## 3. Card Synergies & Combos",
        "- List every meaningful synergy/combo pair or chain",
        "- Sequencing matters: what to cast FIRST for max value",
        "- Lifegain engines, token generators, anthem effects",
        "- ETB triggers, death triggers, combat interactions",
        "- Include power level estimate (game-winning / strong / marginal)",
        "",
        "## 4. Mulligan Guide",
        "- What makes a keepable 7-card hand for this deck?",
        "- Ideal opening hand composition (lands, 1-drops, 2-drops, removal)",
        "- Auto-keep hands (describe 3 example hands)",
        "- Auto-mulligan hands (describe 3 examples)",
        "- 6-card and 5-card keep thresholds",
        "- Key cards that make a borderline hand keepable",
        "",
        "## 5. Phase-by-Phase Play Patterns",
        "- Turn 1 priorities",
        "- Turn 2-3 development",
        "- Mid-game (turns 4-6)",
        "- Late game plan",
        "- When to be aggressive vs defensive",
        "- Combat tricks and instant-speed lines",
        "",
        "## 6. Matchup Guide",
        "For each major Standard meta deck (at least 5):",
        "- Matchup difficulty (favorable/even/unfavorable)",
        "- Their key threats you must answer",
        "  - For each threat: removal_priority (1=must remove NOW, 2=high, 3=when convenient)",
        "  - Mark must_answer=true for threats that win the game if unanswered for 2 turns",
        "- Your best cards in the matchup",
        "- Game plan adjustments (race? control the board? go wide?)",
        "- Their burst damage from hand (hidden reach)",
        "- Cards to watch for (board wipes, counters, removal)",
        "- Signal cards: how to identify this opponent deck early",
        "",
        "## 7. Meta Deck Recognition",
        "For each opponent deck:",
        "- 3-5 signal cards that identify the deck",
        "- Deck speed and kill turn",
        "- Archetype and colors",
        "",
        "## 8. Deck Vulnerabilities",
        "List 3-6 cards from the meta that specifically counter THIS deck's strategy:",
        "- Board wipes that wreck your board (e.g., Day of Judgment vs creature-heavy decks)",
        "- Hate cards that shut down your plan (e.g., graveyard exile vs recursion)",
        "- Enchantment/artifact removal if your deck relies on those",
        "- Cards that bypass your defensive strategy",
        "For each: card name, why it's devastating, severity (critical/high/medium)",
        "",
        "## 9. Common Mistakes to Avoid",
        "- Sequencing errors specific to this deck",
        "- Overextension traps",
        "- Cards you should NOT use for removal (save for face, etc.)",
        "- Tempo mistakes",
        "",
        "Write the guide in markdown. Be specific — name actual cards, real matchups,",
        "concrete examples. This guide will be fed to another AI to generate machine-readable",
        "play advisor rules, so detail and precision matter more than readability.",
        "",
        "IMPORTANT: Section 8 (Deck Vulnerabilities) is critical for the advisor's threat",
        "detection system. Think carefully about which meta cards specifically counter this",
        "deck's win condition and strategy.",
    ])


# ─── Step 1.5: Validate Prompt ────────────────────────────────────

def _build_validate_prompt(deck_path: str, deck_name: str,
                           guide_path: Path, card_names: list[str]) -> str:
    unique_cards = sorted(set(card_names))
    card_list_str = ", ".join(unique_cards)

    return "\n".join([
        "You are a senior MTG strategy editor reviewing a deck guide for completeness and accuracy.",
        "The guide will be used to generate machine-readable rules for a real-time play advisor.",
        "Missing or vague information = bad rules = bad advice during games.",
        "",
        "IMPORTANT: Do NOT use any tools. Do NOT search the web.",
        f"Read the deck list from: {deck_path}",
        f"Read the guide to review from: {guide_path}",
        "",
        f"Deck: {deck_name}",
        f"Cards in deck ({len(unique_cards)} unique non-land): {card_list_str}",
        "",
        "=== REVIEW CHECKLIST ===",
        "",
        "Check each section and report ONLY problems. For each issue give a concrete fix.",
        "",
        "1. **Deck Overview**: Is archetype, speed, win condition, strengths/weaknesses covered?",
        "2. **Key Cards**: Does EVERY non-land card in the deck have a role, casting priority, and synergies listed?",
        f"   All {len(unique_cards)} unique non-land cards must be covered: {card_list_str}",
        "3. **Synergies**: Are ALL meaningful 2-card and 3-card combos identified?",
        "   Check: lifegain payoffs, ETB chains, sequencing (what to cast FIRST), anthem/lord effects,",
        "   protection combos, evasion synergies. Each synergy needs: cards involved, sequencing, power level.",
        "4. **Mulligan Guide**: Are there concrete auto-keep and auto-mulligan examples?",
        "   Are 6-card and 5-card thresholds defined? Are key cards that swing borderline hands listed?",
        "5. **Play Patterns**: Turn-by-turn priorities for T1-T4? When to be aggressive vs defensive?",
        "6. **Matchups**: Are at least 5 meta matchups covered? Each needs:",
        "   - Signal cards for deck identification",
        "   - Their key threats YOU must answer (with YOUR cards)",
        "   - Your best cards in the matchup",
        "   - Game plan adjustment",
        "   - Hidden reach / burst damage estimate",
        "7. **Meta Deck Recognition**: 3-5 signal cards per opponent deck?",
        "8. **Deck Vulnerabilities**: Are 3-6 meta cards listed that specifically counter this deck?",
        "   Each needs: card name, why it's devastating, severity (critical/high/medium)",
        "9. **Accuracy**: Are card names correct? Are abilities described accurately?",
        "   Are interactions rules-correct (e.g., ward costs, exile vs destroy, ETB timing)?",
        "",
        "=== OUTPUT FORMAT ===",
        "",
        "If the guide passes all checks with only minor issues:",
        "  VERDICT: PASS",
        "",
        "If there are significant gaps or errors, output:",
        "  VERDICT: REVISE",
        "  REMARKS:",
        "  - [Section X] Specific issue and what to add/fix",
        "  - [Section Y] Another issue...",
        "",
        "Be strict. A guide that's 'good enough' still needs REVISE if important synergies",
        "or matchups are missing. The rules engine needs exhaustive coverage.",
    ])


# ─── Step 1 Retry Prompt ─────────────────────────────────────────

def _build_research_retry_prompt(deck_path: str, deck_name: str,
                                 remarks_path: Path) -> str:
    return "\n".join([
        "You are an expert MTG Arena competitive coach and deck analyst.",
        "You previously wrote a strategy guide that was reviewed. The reviewer found issues.",
        "Your task: Write a NEW, COMPLETE guide fixing ALL the issues listed below.",
        "",
        "USE YOUR WEB SEARCH TOOLS if you need to look up specific card interactions,",
        "meta decks, or matchup data mentioned in the remarks.",
        "",
        f"Deck name: {deck_name}",
        f"Read the deck list from: {deck_path}",
        f"Read the reviewer remarks from: {remarks_path}",
        "Fix ALL issues listed in the remarks.",
        "",
        "=== GUIDE STRUCTURE (follow exactly) ===",
        "",
        "Write a complete guide with sections ## 1 through ## 9:",
        "1. Deck Overview (archetype, speed, win condition, strengths, weaknesses)",
        "2. Key Cards & Roles (EVERY non-land card: role, when to cast, synergies, protection priority)",
        "3. Card Synergies & Combos (every meaningful pair/chain, sequencing, power level)",
        "4. Mulligan Guide (ideal hands, auto-keep, auto-mull, 6/5-card thresholds, key cards)",
        "5. Phase-by-Phase Play Patterns (T1-T4 priorities, aggressive vs defensive, instant-speed lines)",
        "6. Matchup Guide (5+ meta matchups: difficulty, threats, best cards, plan, burst damage, signals)",
        "7. Meta Deck Recognition (signal cards per opponent deck, speed, archetype)",
        "8. Deck Vulnerabilities (3-6 meta cards that counter this deck's strategy, with severity)",
        "9. Common Mistakes to Avoid",
        "",
        "Be specific — name actual cards, concrete examples, real matchups.",
        "This guide feeds another AI that generates machine-readable rules, so precision matters.",
    ])


# ─── Step 2: Compile Prompt ──────────────────────────────────────

def _build_compile_prompt(deck_path: str, deck_name: str,
                          guide_path: Path) -> str:
    return "\n".join([
        "You are a software engineer converting a strategy guide into machine-readable rules.",
        "Generate a complete strategy JSON file that encodes the guide's knowledge as rules.",
        "",
        "IMPORTANT: Do NOT search the web. Read the files below, then output ONLY valid JSON.",
        "No markdown fences, no explanation, no commentary — ONLY the JSON object.",
        "",
        f"Deck name: {deck_name}",
        f"Read the deck list from: {deck_path}",
        f"Read the strategy guide from: {guide_path}",
        f"Read the JSON rule contract from: {CONTRACT_PATH}",
        "",
        "=== GENERATION TARGETS ===",
        "",
        "Generate rules across ALL layers (aim for 30-50 rules total):",
        "- archetype: 4-6 rules for this deck's playstyle",
        "- mulligan: 6-10 rules (encode the mulligan guide — lands, curve, key cards)",
        "- card_synergy: 8-15 rules (encode EVERY synergy from the guide)",
        "- threat_response: 4-6 rules (how to use YOUR removal on THEIR threats)",
        "- situation: 4-6 rules (life totals, board state, racing, topdeck)",
        "- meta_gameplan: 4-8 rules (matchup-specific — use conflicts_with to override lower layers)",
        "",
        "DO NOT generate 'general' layer rules — those come from general.json automatically.",
        "Instead, list which general rules this deck OVERRIDES in 'general_overrides'.",
        "",
        "Generate 5-8 MetaDeck entries from the matchup guide into a separate 'meta_decks' key.",
        "These will be saved to the GLOBAL meta_decks.json (shared across all decks).",
        "Each needs signal_cards (weights sum ~1.0), key_threats with enriched fields, speed, hidden_reach.",
        "",
        "=== KEY_THREATS FORMAT ===",
        "",
        "Each key_threat in a MetaDeck MUST include:",
        '  {"card": "Name", "danger": "high", "reason": "Why dangerous",',
        '   "removal_priority": 1, "must_answer": true}',
        "- removal_priority: 1=must remove NOW, 2=high priority, 3=remove when convenient",
        "- must_answer: true if unanswered for ~2 turns = game loss",
        "",
        "=== GENERAL_OVERRIDES ===",
        "",
        "general.json provides these rules that merge into every deck at runtime:",
        "- general_play_land, general_attack_before_main2, general_hold_instant",
        "- general_dont_overextend, general_low_life_caution, general_opp_low_life",
        "- general_save_removal, general_curve_out",
        "- mulligan_no_lands, mulligan_one_land, mulligan_flood, mulligan_good_hand",
        "- meta_vs_fast_hold_blockers, meta_vs_slow_push_damage",
        "- meta_must_answer_on_board, meta_vulnerability_warning",
        "",
        "List any of these IDs in 'general_overrides' if your deck replaces them with",
        "its own variant. For example, aggro decks override general_hold_instant.",
        "If your deck has its own mulligan rules, override the general mulligan rules.",
        "",
        "=== VULNERABILITIES ===",
        "",
        "From the guide's Deck Vulnerabilities section, generate the 'vulnerabilities' array:",
        '  {"card": "Day of Judgment", "reason": "Full board wipe", "severity": "critical"}',
        "Include 3-6 cards that specifically counter THIS deck's strategy.",
        "",
        "=== RULES ===",
        "",
        "IMPORTANT:",
        "- Every synergy from the guide should become at least one card_synergy rule",
        "- Every matchup should produce at least one meta_gameplan rule",
        "- Mulligan rules should be specific: check for actual cards, not just generic counts",
        "- Use {card} and {threat} placeholders in action text",
        "- Action text must be SHORT (<80 chars)",
        "- Rules MUST use correct card names from the deck list",
        "- Set weight 1.2-1.5 for critical synergies and key plays",
        "",
        "Output ONLY the JSON object.",
    ])


# ─── Shared Utilities ────────────────────────────────────────────

def _parse_deck_list(path: str) -> tuple[str, list[str]]:
    """Parse MTGA deck list file. Returns (raw_text, card_names)."""
    text = Path(path).read_text()
    card_names = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("Deck") or line.startswith("Sideboard") or line.startswith("Commander"):
            continue
        m = re.match(r"(\d+)\s+(.+?)\s+\(", line)
        if m:
            count = int(m.group(1))
            name = m.group(2).strip()
            card_names.extend([name] * count)
        else:
            m = re.match(r"(\d+)\s+(.+)", line)
            if m:
                count = int(m.group(1))
                name = m.group(2).strip()
                card_names.extend([name] * count)
    return text, card_names


def _safe_name(name: str) -> str:
    return name.replace(" ", "_").replace("/", "_").replace("'", "").lower()


def _guide_path(name: str) -> Path:
    return RULES_DIR / f"{_safe_name(name)}_guide.md"


def _strategy_path(name: str) -> Path:
    return RULES_DIR / f"{_safe_name(name)}.json"


def _repair_json(text: str) -> str:
    """Fix common LLM JSON errors."""
    text = re.sub(r',\s*([}\]])', r'\1', text)
    text = re.sub(r'"card"\s*:\s*"([^"]+)"\s*:\s*[\d.]+', r'"card":"\1"', text)
    return text


def _extract_json(text: str) -> dict | None:
    """Extract JSON from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip().startswith("```"):
                end = i
                break
        text = "\n".join(lines[start:end])

    brace_start = text.find("{")
    if brace_start == -1:
        return None

    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                json_str = text[brace_start:i + 1]
                try:
                    return json.loads(json_str)
                except json.JSONDecodeError:
                    repaired = _repair_json(json_str)
                    try:
                        log.info("JSON repaired successfully")
                        return json.loads(repaired)
                    except json.JSONDecodeError as e:
                        log.error("JSON parse error after repair: %s", e)
                        return None
    return None


def _validate_strategy(data: dict) -> list[str]:
    """Validate strategy JSON. Returns list of errors."""
    errors = []
    if "name" not in data:
        errors.append("Missing 'name'")
    if "rules" not in data or not isinstance(data.get("rules"), list):
        errors.append("Missing or invalid 'rules' array")
        return errors

    valid_layers = {"general", "archetype", "mulligan", "card_synergy",
                    "threat_response", "situation", "meta_gameplan"}
    valid_priorities = {"critical", "high", "medium", "low"}
    valid_zones = {"hand", "battlefield", "opp_battlefield", "graveyard",
                   "opp_graveyard", "stack"}

    seen_ids = set()
    for i, rule in enumerate(data["rules"]):
        prefix = f"Rule [{i}]"
        if "id" not in rule:
            errors.append(f"{prefix}: missing 'id'")
        elif rule["id"] in seen_ids:
            errors.append(f"{prefix}: duplicate id '{rule['id']}'")
        else:
            seen_ids.add(rule["id"])

        if rule.get("layer") not in valid_layers:
            errors.append(f"{prefix} ({rule.get('id', '?')}): invalid layer '{rule.get('layer')}'")
        if rule.get("priority") and rule["priority"] not in valid_priorities:
            errors.append(f"{prefix} ({rule.get('id', '?')}): invalid priority '{rule['priority']}'")
        if not rule.get("action"):
            errors.append(f"{prefix} ({rule.get('id', '?')}): missing 'action'")
        for j, req in enumerate(rule.get("require", [])):
            if req.get("zone") not in valid_zones:
                errors.append(f"{prefix} require[{j}]: invalid zone '{req.get('zone')}'")

    for i, md in enumerate(data.get("meta_decks", [])):
        if "name" not in md:
            errors.append(f"MetaDeck [{i}]: missing 'name'")
        if "signal_cards" not in md or not md["signal_cards"]:
            errors.append(f"MetaDeck [{i}] ({md.get('name', '?')}): missing signal_cards")

    return errors


def _ensure_defaults(data: dict) -> dict:
    for rule in data.get("rules", []):
        rule.setdefault("weight", 1.0)
        rule.setdefault("priority", "medium")
        rule.setdefault("stats", {"fired": 0, "correct": 0})
        rule.setdefault("tags", [])
        rule.setdefault("conflicts_with", [])
    data.setdefault("stats", {"games": 0, "wins": 0, "losses": 0})
    data.setdefault("deck_signature", [])
    data.setdefault("colors", [])
    data.setdefault("archetype", "unknown")
    data.setdefault("general_overrides", [])
    data.setdefault("vulnerabilities", [])
    return data


# ─── LLM Backends ────────────────────────────────────────────────

async def _call_claude_cli(prompt: str, max_turns: int = 3,
                           timeout: float = 300.0,
                           allow_web_search: bool = False,
                           model: str | None = None) -> str:
    """Call Claude CLI subprocess."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # Allow Read tool + web search for research; Read-only for compile/validate
    allowed = "Read,WebSearch,WebFetch" if allow_web_search else "Read"
    data_dir = str(RULES_DIR.parent.resolve())  # ~/MTG/advisor/data
    home = str(Path.home() / "MTG")

    cmd = [
        "claude", "-p", prompt,
        "--max-turns", str(max_turns),
        "--allowedTools", allowed,
        "--add-dir", data_dir,
        "--add-dir", home,
    ]
    if model:
        cmd.extend(["--model", model])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Claude CLI timeout ({timeout:.0f}s)")

    if proc.returncode != 0:
        err = stderr.decode().strip()
        if "Reached max turns" not in err:
            raise RuntimeError(f"Claude CLI error: {err[:500]}")

    return stdout.decode().strip()


async def _call_anthropic_api(prompt: str, max_tokens: int = 8000) -> str:
    import anthropic
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text if response.content else ""


async def _call_llm(prompt: str, backend: str, max_turns: int = 3,
                    timeout: float = 300.0,
                    allow_web_search: bool = False,
                    model: str | None = None) -> str:
    if backend == "claude_cli":
        return await _call_claude_cli(prompt, max_turns, timeout,
                                      allow_web_search=allow_web_search,
                                      model=model)
    elif backend == "anthropic_api":
        return await _call_anthropic_api(prompt)
    raise RuntimeError(f"Unknown backend: {backend}")


# ─── Step 1: Research ────────────────────────────────────────────

async def _run_research(deck_path: str, deck_name: str,
                        backend: str, prompt: str) -> str:
    """Run a single research LLM call. Returns guide text."""
    print("  Calling LLM (web search, guides, meta analysis)...")
    print("  This may take 3-10 minutes...")
    response = await _call_llm(prompt, backend, max_turns=15, timeout=900.0,
                               allow_web_search=True, model="sonnet")
    print(f"  Guide: {len(response)} chars, {len(response.splitlines())} lines")
    return response


async def _validate_guide(deck_path: str, deck_name: str,
                          guide_path: Path, card_names: list[str],
                          backend: str) -> tuple[bool, str]:
    """Validate guide quality. Returns (passed, remarks)."""
    print("\n  [Validate] Reviewing guide quality...")
    prompt = _build_validate_prompt(deck_path, deck_name, guide_path, card_names)
    response = await _call_llm(prompt, backend, max_turns=3, timeout=180.0)

    # Parse verdict
    passed = "VERDICT: PASS" in response.upper() or "VERDICT:PASS" in response.upper()
    remarks = response

    if passed:
        print("  [Validate] PASS — guide quality is sufficient")
    else:
        # Extract just the remarks section
        remarks_start = response.find("REMARKS:")
        if remarks_start != -1:
            remarks = response[remarks_start:]
        # Count issues
        issues = [l for l in remarks.splitlines()
                  if l.strip().startswith("-") or l.strip().startswith("*")]
        print(f"  [Validate] REVISE — {len(issues)} issues found:")
        for issue in issues[:8]:
            print(f"    {issue.strip()}")
        if len(issues) > 8:
            print(f"    ... and {len(issues) - 8} more")

    return passed, remarks


def _save_guide(guide: str, deck_name: str) -> Path:
    """Save guide to file. Protects against regression (won't overwrite longer guide with shorter)."""
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    guide_file = _guide_path(deck_name)

    if guide_file.exists():
        existing = guide_file.read_text()
        if len(guide) < len(existing) * 0.5:
            print(f"  [Warning] New guide ({len(guide)} chars) is much shorter "
                  f"than existing ({len(existing)} chars) — keeping existing")
            guide = existing

    guide_file.write_text(guide)

    sections = [l for l in guide.splitlines() if l.startswith("## ")]
    print(f"  Sections: {len(sections)}")
    for s in sections:
        print(f"    {s}")
    print(f"  Guide saved: {guide_file}")
    return guide_file


async def step_research(deck_path: str, deck_name: str,
                        backend: str = "claude_cli",
                        max_retries: int = 1) -> Path:
    """Step 1: Generate strategy guide, validate, optionally retry with remarks."""
    _, card_names = _parse_deck_list(deck_path)
    if not card_names:
        raise ValueError(f"No cards found in {deck_path}")

    # Resolve to absolute path for Claude CLI file reading
    abs_deck_path = str(Path(deck_path).resolve())

    unique = sorted(set(c for c in card_names
                        if not any(w in c.lower() for w in ["plains", "island", "swamp", "mountain", "forest"])))
    print(f"[Step 1] RESEARCH: {deck_name}")
    print(f"  Cards: {len(card_names)} ({len(unique)} unique non-land)")
    print(f"  Deck file: {abs_deck_path}")
    print(f"  Backend: {backend}")
    print(f"  Max retries: {max_retries}")
    print()

    # Initial research
    prompt = _build_research_prompt(abs_deck_path, deck_name)
    guide = await _run_research(abs_deck_path, deck_name, backend, prompt)
    guide_file = _save_guide(guide, deck_name)

    # Validate + retry loop
    for attempt in range(max_retries):
        passed, remarks = await _validate_guide(
            abs_deck_path, deck_name, guide_file, card_names, backend)

        if passed:
            break

        # Save remarks to file for retry prompt
        remarks_file = RULES_DIR / f"{_safe_name(deck_name)}_remarks.txt"
        remarks_file.write_text(remarks)

        print(f"\n  [Retry {attempt + 1}/{max_retries}] Rewriting guide with reviewer feedback...")
        retry_prompt = _build_research_retry_prompt(
            abs_deck_path, deck_name, remarks_file)
        guide = await _run_research(abs_deck_path, deck_name, backend, retry_prompt)
        guide_file = _save_guide(guide, deck_name)
    else:
        # Final validation after last retry (informational only)
        passed, remarks = await _validate_guide(
            abs_deck_path, deck_name, guide_file, card_names, backend)
        if not passed:
            print("\n  [Note] Guide still has issues after retries — proceeding anyway")

    return guide_file


# ─── Step 2: Compile ─────────────────────────────────────────────

async def step_compile(deck_path: str, deck_name: str,
                       guide_path: Path | None = None,
                       backend: str = "claude_cli",
                       output: str | None = None) -> Path:
    """Step 2: Compile guide + deck into JSON strategy rules."""
    _, card_names = _parse_deck_list(deck_path)
    if not card_names:
        raise ValueError(f"No cards found in {deck_path}")

    abs_deck_path = str(Path(deck_path).resolve())

    # Load guide
    if guide_path is None:
        guide_path = _guide_path(deck_name)
    if not guide_path.exists():
        raise FileNotFoundError(
            f"No guide found at {guide_path}. Run --step research first.")

    guide = guide_path.read_text()
    print(f"[Step 2/2] COMPILE: {deck_name}")
    print(f"  Guide: {guide_path.name} ({len(guide)} chars)")
    print(f"  Deck file: {abs_deck_path}")
    print(f"  Contract: {CONTRACT_PATH}")
    print(f"  Backend: {backend}")
    print()

    prompt = _build_compile_prompt(abs_deck_path, deck_name, guide_path)

    print("  Generating JSON rules from guide...")
    print("  This may take 3-10 minutes...")
    response = await _call_llm(prompt, backend, max_turns=10, timeout=900.0)

    print(f"  Response: {len(response)} chars")

    # Extract JSON
    data = _extract_json(response)
    if not data:
        debug_path = RULES_DIR / "last_failed_response.txt"
        debug_path.write_text(response)
        raise ValueError(
            f"Failed to extract JSON. Raw saved to: {debug_path}")

    # Validate
    errors = _validate_strategy(data)
    if errors:
        print(f"\n  Validation issues ({len(errors)}):")
        for e in errors[:10]:
            print(f"    - {e}")
        fatal = [e for e in errors
                 if "missing 'id'" in e or "missing 'action'" in e]
        if fatal:
            debug_path = RULES_DIR / "last_invalid_response.json"
            debug_path.write_text(json.dumps(data, indent=2))
            raise ValueError(
                f"Fatal validation errors. JSON saved to: {debug_path}")
        print("    (non-fatal — proceeding)")

    data = _ensure_defaults(data)
    data["name"] = deck_name

    # Extract meta_decks from LLM output → merge into global file
    new_meta = data.pop("meta_decks", [])
    if new_meta:
        from .strategy import load_meta_decks, MetaDeck, save_meta_decks
        existing = load_meta_decks()
        existing_names = {md.name for md in existing}
        added = 0
        for md_dict in new_meta:
            if md_dict.get("name") not in existing_names:
                existing.append(MetaDeck(
                    name=md_dict["name"],
                    archetype=md_dict.get("archetype", "unknown"),
                    colors=md_dict.get("colors", []),
                    signal_cards=md_dict.get("signal_cards", {}),
                    key_threats=md_dict.get("key_threats", []),
                    speed=md_dict.get("speed", "medium"),
                    typical_kill_turn=md_dict.get("typical_kill_turn", 10),
                    hidden_reach=md_dict.get("hidden_reach", 0),
                    description=md_dict.get("description", ""),
                ))
                added += 1
        if added:
            save_meta_decks(existing)
            print(f"  Meta decks: {added} new → global meta_decks.json "
                  f"(total: {len(existing)})")

    # Save strategy (rules only, no meta_decks)
    if output:
        out_path = Path(output)
    else:
        out_path = _strategy_path(deck_name)
    out_path.write_text(json.dumps(data, indent=2))

    _print_summary(data, out_path)
    return out_path


def _print_summary(data: dict, path: Path):
    rules = data.get("rules", [])
    layer_counts: dict[str, int] = {}
    for r in rules:
        layer_counts[r.get("layer", "?")] = layer_counts.get(
            r.get("layer", "?"), 0) + 1

    print(f"\n  Strategy saved: {path}")
    print(f"  Name: {data['name']}")
    print(f"  Archetype: {data.get('archetype', '?')}")
    print(f"  Colors: {', '.join(data.get('colors', []))}")
    print(f"  Signature: {', '.join(data.get('deck_signature', []))}")
    print(f"  Rules: {len(rules)} total")
    for layer in ["general", "archetype", "mulligan", "card_synergy",
                   "threat_response", "situation", "meta_gameplan"]:
        if layer in layer_counts:
            print(f"    {layer}: {layer_counts[layer]}")
    # Show global meta_decks count
    meta_path = RULES_DIR / "meta_decks.json"
    if meta_path.exists():
        try:
            meta_data = json.loads(meta_path.read_text())
            meta_count = len(meta_data.get("meta_decks", []))
            print(f"  Global meta decks: {meta_count} (meta_decks.json)")
        except Exception:
            pass


# ─── Full Pipeline ────────────────────────────────────────────────

async def run_pipeline(deck_path: str, deck_name: str,
                       step: str = "both",
                       backend: str = "claude_cli",
                       output: str | None = None,
                       max_retries: int = 1):
    """Run the full research → validate → (retry) → compile pipeline."""
    if step in ("research", "both"):
        await step_research(deck_path, deck_name, backend,
                            max_retries=max_retries)
        print()

    if step in ("compile", "both"):
        await step_compile(deck_path, deck_name,
                           backend=backend, output=output)


# ─── List Command ─────────────────────────────────────────────────

def list_strategies():
    if not RULES_DIR.exists():
        print("No strategies directory found.")
        return

    json_files = sorted(f for f in RULES_DIR.glob("*.json")
                        if f.name != "meta_decks.json")
    md_files = sorted(RULES_DIR.glob("*_guide.md"))

    if not json_files and not md_files:
        print("No strategy files found.")
        return

    print(f"Strategies in {RULES_DIR}:\n")

    # Show global meta_decks
    meta_path = RULES_DIR / "meta_decks.json"
    if meta_path.exists():
        try:
            meta_data = json.loads(meta_path.read_text())
            mds = meta_data.get("meta_decks", [])
            print(f"  Global meta_decks.json ({len(mds)} opponent decks)")
            for md in mds:
                print(f"    - {md['name']} ({md.get('archetype', '?')}, "
                      f"speed: {md.get('speed', '?')})")
            print()
        except Exception:
            pass

    for path in json_files:
        if path.name.startswith("last_"):
            continue
        try:
            data = json.loads(path.read_text())
            rules = data.get("rules", [])
            stats = data.get("stats", {})
            games = stats.get("games", 0)
            wr = stats.get("wins", 0) / games * 100 if games else 0

            layer_counts: dict[str, int] = {}
            for r in rules:
                layer_counts[r.get("layer", "?")] = (
                    layer_counts.get(r.get("layer", "?"), 0) + 1)

            # Check if guide exists
            name = data.get("name", path.stem)
            guide = _guide_path(name)
            has_guide = guide.exists()

            print(f"  {path.name}" +
                  (f" + {guide.name}" if has_guide else " (no guide)"))
            print(f"    Name: {name}")
            print(f"    Archetype: {data.get('archetype', '?')} | "
                  f"Colors: {', '.join(data.get('colors', []))}")
            print(f"    Rules: {len(rules)}")
            print(f"    Games: {games} | Win rate: {wr:.0f}%")
            print(f"    Layers: {', '.join(f'{k}:{v}' for k, v in sorted(layer_counts.items()))}")
            print()
        except Exception as e:
            print(f"  {path.name} — ERROR: {e}\n")

    # Show orphan guides (research done but not yet compiled)
    for md_path in md_files:
        json_name = md_path.name.replace("_guide.md", ".json")
        if not (RULES_DIR / json_name).exists():
            lines = md_path.read_text().splitlines()
            print(f"  {md_path.name} (guide only — run --step compile)")
            print(f"    Lines: {len(lines)}")
            print()


# ─── CLI ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Two-step LLM pipeline: research → compile deck strategy rules")
    parser.add_argument("--deck", "-d",
                        help="Path to MTGA deck list file")
    parser.add_argument("--name", "-n",
                        help="Strategy name (default: derived from filename)")
    parser.add_argument("--step", "-s", default="both",
                        choices=["research", "compile", "both"],
                        help="Which step to run (default: both)")
    parser.add_argument("--backend", "-b", default="claude_cli",
                        choices=["claude_cli", "anthropic_api"],
                        help="LLM backend (default: claude_cli)")
    parser.add_argument("--output", "-o",
                        help="Output JSON path (step 2)")
    parser.add_argument("--retries", "-r", type=int, default=1,
                        help="Max research retries after validation (default: 1)")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List existing strategies and guides")
    parser.add_argument("--prompt-only", action="store_true",
                        help="Print prompts without calling LLM")

    args = parser.parse_args()

    if args.list:
        list_strategies()
        return

    if not args.deck:
        parser.error("--deck is required (unless using --list)")

    deck_name = (args.name or
                 Path(args.deck).stem.replace("_", " ").replace("-", " ").title())

    if args.prompt_only:
        abs_deck_path = str(Path(args.deck).resolve())
        if args.step in ("research", "both"):
            print("=== RESEARCH PROMPT ===\n")
            print(_build_research_prompt(abs_deck_path, deck_name))
        if args.step in ("compile", "both"):
            guide_path = _guide_path(deck_name)
            print("\n=== COMPILE PROMPT ===\n")
            print(_build_compile_prompt(abs_deck_path, deck_name, guide_path))
        return

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    asyncio.run(run_pipeline(
        args.deck, deck_name,
        step=args.step,
        backend=args.backend,
        output=args.output,
        max_retries=args.retries,
    ))


if __name__ == "__main__":
    main()
