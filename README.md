# Scryglass — MTGA Real-Time Play Advisor

Real-time play advice for Magic: The Gathering Arena. Reads game logs, identifies opponent's deck, and suggests optimal plays turn by turn.

## What it does

- **Play advice** — suggests what to cast, when to attack, threat warnings
- **Opponent identification** — predicts opponent's deck after 1-2 cards, shows expected threats
- **Rule engine** — 600+ rules across deck-specific strategies + universal MTG fundamentals
- **Deck management** — lifecycle with versioning, rule generation, GA optimization
- **Web UI** — real-time dashboard with Focus/Tactical/Full view profiles

## Installing the desktop app

Download the latest installer from
[Releases](https://github.com/vbalko-claimate/scryglass/releases/latest):
`Scryglass_<version>_x64_en-US.msi` (Windows) or `Scryglass_<version>_aarch64.dmg` (macOS).

### Windows: SmartScreen will warn you, and it is right to

The installer is **not code-signed** — a certificate costs money and the app is
pre-alpha — so Windows has no publisher to check and shows *"Windows protected
your PC"*. To install anyway: **More info → Run anyway**. Some antivirus
products flag any new unsigned installer for the same reason; that is a
reputation check, not a detection.

If that trade is not acceptable to you, do not install it. Saying "just click
through the warning" without saying why the warning exists would be teaching a
bad habit — the warning is the system working.

Once installed, Scryglass lives in the system tray. If something looks wrong,
**tray → Open Diagnostics Log** — it works even when the backend is down, which
is exactly when you need it.

### What it needs from MTGA

* **Detailed Logs** must be on: MTGA → Settings → Account → *Detailed Logs
  (Plugin Support)*, then **restart MTGA**. Without it the log carries no match
  data and no advisor can see your game.
* The advisor's card knowledge is **Standard**. In Historic, Alchemy or Timeless
  most of your deck is outside it, and the app says so rather than guessing.

## Quick Start

```bash
# Clone
git clone https://github.com/vbalko-claimate/scryglass.git
cd scryglass

# Install (requires Python 3.12+ and uv)
uv sync

# Run
uv run python run.py
# Open http://localhost:8765
```

Start MTGA, play a game — Scryglass picks up the log automatically.

## Requirements

- **Python 3.12+**
- **uv** — `pip install uv` or `brew install uv`
- **MTGA** installed (Steam or standalone)
  - macOS: `~/Library/Logs/Wizards Of The Coast/MTGA/Player.log`
  - Windows: `%USERPROFILE%/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log`

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCRY_USER_DATA` | `~/MTG/mtg-data` | User data directory (decks, strategies) |
| `ANTHROPIC_API_KEY` | — | Claude API key (optional, for LLM advice) |
| `SCRY_ADVICE_MODE` | `hybrid` | `hybrid`, `llm_first`, or `llm_only` |

Copy `.env.example` to `.env` and fill in values as needed. The app works without any API keys — rule engine and heuristics run locally.

### User Data Directory

Scryglass stores user decks and strategies in `SCRY_USER_DATA` (default `~/MTG/mtg-data`):

```
mtg-data/
  decks/
    mono_red_goblins/
      deck.json           # metadata + decklist
      strategy.json       # deployed rules (active)
      versions/           # version history
      ga_logs/            # GA optimization logs
      guides/             # strategy guides (.md)
```

### LLM Backends

The advisor can use LLMs for supplementary advice (optional):

1. **Claude CLI** — subscription-based, auto-detected if `claude` is in PATH
2. **Ollama** — free local LLM, auto-detected at `localhost:11434`
3. **Anthropic API** — pay-per-use, requires `ANTHROPIC_API_KEY`

Without any LLM, the rule engine + heuristics provide full play advice.

## Architecture

```
MTGA Player.log → LogWatcher → GameStateTracker → AdvisorEngine → WebSocket → Browser UI
                                                       ↓
                                              Strategy (rules) + Heuristics
```

- **LogWatcher** — tails MTGA log, parses GRE messages
- **GameStateTracker** — maintains game state (battlefield, hand, life totals)
- **AdvisorEngine** — orchestrates rule engine, heuristics, LLM, threat radar
- **Strategy** — deck-specific rules loaded from `decks/{id}/strategy.json`
- **Heuristics** — board-aware tactical advice (lethal detection, combat math, mana efficiency)

## Testing

```bash
# Deck lifecycle tests (79 assertions)
uv run python -m advisor.test_deck_lifecycle

# Full CI gate check (canonical actions, regression, replay, schema)
uv run python -m advisor.ci_check

# Health check (3-tier canary)
uv run python -m advisor.health_check
```

## Pages

| URL | Description |
|-----|-------------|
| `/` | Main advisor — real-time play advice during MTGA match |
| `/manage` | Strategy editor, deck browser, GA runs, guides |
| `/decks` | Deck lifecycle management (create, version, deploy) |
| `/stats` | Match history and statistics |

## How It Works (No Cheating)

Scryglass reads only `Player.log` — the same log file that Untapped.gg, Arena Tutor, and other community tools read. It does NOT:
- Modify game files or memory
- Inject code into MTGA process
- Send automated inputs to the game
- Access information not visible to the player

## Optional: GA Optimization (mtg-simlab)

For advanced users: genetic algorithm optimization of rule weights via [mtg-simlab](https://github.com/vbalko-claimate/mtg-simlab) + Forge simulator. Requires Java 17+ and Docker. See mtg-simlab README for setup.

## License

MIT
