## Strategy JSON Contract

The strategy file must be valid JSON with this structure:

```json
{
  "name": "Deck Name Archetype",
  "deck_signature": ["Card1", "Card2", "Card3"],  // 3-5 most important non-land cards
  "colors": ["W", "R"],  // deck colors: W, U, B, R, G
  "archetype": "aggro",  // aggro, midrange, control, combo
  "general_overrides": ["general_play_land", "mulligan_flood"],  // IDs from general.json this deck replaces
  "vulnerabilities": [ ... ],  // cards that threaten THIS deck's strategy (see below)
  "rules": [ ... ],      // array of Rule objects (see below)
  "stats": {"games": 0, "wins": 0, "losses": 0}
}
```

**Note:** `meta_decks` are stored in a GLOBAL `meta_decks.json` file, NOT per-deck.

### General Rules Merge

A `general.json` file contains universal MTG rules (play a land, attack before Main 2,
mulligan rules, etc.). These merge into EVERY deck at runtime automatically.

- If a deck has its own rule with the same `id` as a general rule, the general rule is skipped.
- If a deck lists a general rule ID in `general_overrides`, that general rule is skipped
  (the deck provides its own variant via a deck-specific rule with a different ID).
- Deck-specific rules always win over general rules.

Example: An aggro deck overrides `general_hold_instant` because aggro wants to spend
mana proactively, not hold instants. It lists `"general_hold_instant"` in `general_overrides`
and has its own `arch_spend_mana` rule instead.

### Vulnerabilities

Cards that specifically threaten THIS deck's strategy. Used by `meta_vulnerability_warning`
general rule and for threat scoring boost.

```json
"vulnerabilities": [
  {
    "card": "Temporary Lockdown",
    "reason": "Exiles all your 1-2 CMC creatures",
    "severity": "critical"   // critical, high, medium
  },
  {
    "card": "Day of Judgment",
    "reason": "Full board wipe destroys your go-wide plan",
    "severity": "critical"
  }
]
```

Think about what cards in the meta specifically counter YOUR deck's strategy:
- Board wipes vs creature-heavy decks
- Graveyard hate vs recursion decks
- Enchantment removal vs aura/enchantment strategies
- Cards that shut down your win condition

### Rule Object

Each rule is a JSON object:

**Identity:**
- `id` (string, required): Unique, format: `{layer}_{short_name}`
- `layer` (string, required): "general" | "archetype" | "mulligan" | "card_synergy" | "threat_response" | "situation" | "meta_gameplan"
- `tags` (string[]): Optional categories like ["tempo", "protect", "reactive", "sequence"]

**Trigger — when this rule can fire:**
- `phase` (string[]): ["Main"], ["Combat"], ["Main", "Combat"], ["Mulligan"], or omit for any
- `my_turn` (bool|null): true = my turn only, false = opponent's, omit = either
- `turn_min` / `turn_max` (int): Turn range
- `step` (string): "Phase_Main1", "Phase_Main2"

**Zone Conditions — the core matching system:**
- `require` (array): ALL must be true:
  ```json
  {
    "zone": "hand",              // hand, battlefield, opp_battlefield, graveyard, opp_graveyard, stack
    "match": {
      "name": "Card Name",      // exact name (string or ["Name1", "Name2"])
      "keyword": "Lifelink",    // ability keyword (supports pipe: "destroy|exile|damage")
      "type": "Creature",       // Creature, Instant, Sorcery, Enchantment, Artifact, Land
      "cmc_min": 3, "cmc_max": 2,
      "power_min": 4, "toughness_min": 3,  // uses LIVE power/toughness (with buffs)
      "castable": true,         // castable with current mana (checks BOTH CMC and color match)
      "color": "R"
    },
    "min_count": 1, "max_count": 3,
    "absent": true,             // card must NOT be in zone
    "tapped": false,            // true = tapped, false = untapped
    "prefer": "synergy_first"   // optional: re-sort matches by hand synergy (for {card} placeholder)
  }
  ```

**Simple Conditions:**
- `life_below`, `life_above`, `opp_life_below` (int)
- `mana_min` (int): untapped lands
- `hand_lands_min` / `hand_lands_max`, `hand_size_min` / `hand_size_max` (int)
- `hand_castable_min` / `hand_castable_max` (int): castable non-land cards
- `my_creatures_min`, `opp_creatures_min` (int)

**Opponent Meta Conditions (for meta_gameplan rules):**
- `opp_speed` (string): Fires when opponent deck matches speed category.
  Values: "fast" (matches very_fast/fast), "medium" (matches medium_fast/medium), "slow" (matches medium_slow/slow)
- `opp_has_must_answer` (bool): true = fires when opponent has a `must_answer` threat on battlefield
- `opp_has_vulnerability` (bool): true = fires when opponent has a card from YOUR `vulnerabilities` list on battlefield

These require an identified opponent deck (from meta_decks.json) to evaluate.

**Output:**
- `action` (string, required): Short advice. Supports `{card}` and `{threat}` placeholders.
- `action_family` (string, optional): Canonical action type. One of: `cast_spell`, `play_land`, `attack`, `block`, `activate`, `pass`. When set, the engine uses it directly; when omitted, the engine infers from action text. Prefer setting explicitly for unambiguous rules.
- `priority`: "critical" | "high" | "medium" | "low"
- `conflicts_with` (string[], DEPRECATED): Legacy field. Only used for mulligan rules. For non-mulligan hold/use conflicts, the engine auto-detects via action_family. New rules should NOT include conflicts_with.

**Learning (set defaults):**
- `weight` (float): 1.0 neutral. 1.2-1.5 for confident rules.
- `stats`: `{"fired": 0, "correct": 0}`

### MetaDeck Object (global meta_decks.json)

Meta decks are stored in `meta_decks.json` (shared across ALL deck strategies).
Each MetaDeck describes an opponent deck for recognition and threat assessment.

```json
{
  "name": "Mono Red Aggro",
  "archetype": "aggro",
  "colors": ["R"],
  "signal_cards": {"Monastery Swiftspear": 0.4, "Play with Fire": 0.3},
  "key_threats": [
    {
      "card": "Monastery Swiftspear",
      "danger": "high",
      "reason": "Prowess — grows with each spell",
      "removal_priority": 1,
      "must_answer": true
    }
  ],
  "speed": "very_fast",
  "typical_kill_turn": 5,
  "hidden_reach": 6,
  "description": "Burns face"
}
```

**key_threats enriched fields:**
- `removal_priority` (int): 1 = must remove immediately, 2 = high priority, 3 = remove when convenient
- `must_answer` (bool): If true, failing to answer this in ~2 turns = game loss
- `danger` (string): "critical" | "high" | "medium"
- `reason` (string): Why this card is dangerous

### Layer Guide

- **general (0)**: Universal fundamentals. "Play a land", "Attack before Main 2". Provided by `general.json`, merged at runtime.
- **archetype (1)**: Archetype patterns. Aggro: curve out, go wide. Control: hold mana.
- **mulligan (2)**: Hand evaluation. Phase MUST be ["Mulligan"]. Always check lands + castable.
- **card_synergy (3)**: Combos/sequences in YOUR deck. Check multiple zones.
- **threat_response (4)**: React to opponent threats. Use opp_battlefield + hand.
- **situation (5)**: Board state triggers. Life totals, creature counts, racing.
- **meta_gameplan (6)**: Matchup strategy. Can use `opp_speed`, `opp_has_must_answer`, `opp_has_vulnerability`. Highest priority, overrides lower layers.

### Heuristics Engine (automatic — do NOT duplicate in rules)

The heuristics engine runs alongside strategy rules and handles these automatically:
- **Mulligan evaluation**: land count, color availability, early play assessment, tapped lands, aggro/tempo reactive filtering
- **Removal targeting**: best target selection, ward cost calculation, hexproof/shroud/indestructible detection (including from auras), aura removal suggestions
- **Combat math**: lethal detection, double strike power multiplier, blocker assignment
- **Creature prioritization**: curve-out ordering, evasion preference on empty board, flash hold vs deploy
- **Threat warnings**: Seam Rip awareness, opponent speed-based deployment advice

Do NOT create rules for these — the heuristics engine does them better with live game state.
Focus rules on: deck-specific synergies, card combos, matchup gameplans, and sequencing that requires deck knowledge.

### Safety Checks (automatic)

The rule engine applies these checks after a rule fires:
- If `{card}` is in hand and action suggests casting: verifies you have enough mana (suppresses if not)
- If `{card}` is a creature on battlefield and action suggests attacking: verifies it can attack (not summoning sick/tapped)
- If `{threat}` is targeted by removal: warns about hexproof, ward, indestructible
- You do NOT need to add mana checks or protection checks in your rules — the engine handles them.

### Design Principles

1. Rules fire every ~100ms. Keep them simple.
2. More conditions = less false positives. Under-trigger > wrong advice.
3. Hold/use conflicts are auto-detected via `action_family`. Only use `conflicts_with` for mulligan rules.
4. Action text SHORT (<80 chars). Displayed in compact overlay.
5. Mulligan rules: always include `hand_lands_min/max` + `hand_size_min`.
6. Meta decks: 3-5 signal cards, weights sum to ~1.0.
7. Don't duplicate general.json rules — override them via `general_overrides` if needed.
8. `vulnerabilities` should list 3-6 cards that specifically counter YOUR deck's plan.
9. Don't duplicate heuristics engine logic — focus on deck-specific knowledge the engine can't derive.
10. `keyword` supports pipe-separated alternatives: `"destroy|exile|damage"`.
