# Deck Lifecycle Management — Design Document

## 1. Data Model

### Storage: SQLite (in `advisor.db`) + JSON files (unchanged)

Strategy JSON files stay where they are (`mtg-data/strategies/`). The lifecycle
layer is a thin SQLite wrapper that tracks *which* JSON belongs to *which* deck
version and what happened to it over time. No data migration — existing
strategies work as-is; they just aren't "managed" until you import them.

### Tables

```sql
-- A deck is a first-class entity with identity across versions
CREATE TABLE IF NOT EXISTS decks (
    deck_id     TEXT PRIMARY KEY,           -- slug: "rakdos_midrange"
    name        TEXT NOT NULL,              -- "Rakdos Midrange"
    description TEXT DEFAULT '',
    colors      TEXT DEFAULT '[]',          -- JSON array: ["B","R"]
    archetype   TEXT DEFAULT '',            -- auto-detected or manual
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Every card-list change creates a new version
CREATE TABLE IF NOT EXISTS deck_versions (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id         TEXT NOT NULL REFERENCES decks(deck_id),
    version_number  INTEGER NOT NULL,       -- 1, 2, 3...
    deck_list       TEXT NOT NULL,          -- full MTGA import text
    deck_list_hash  TEXT NOT NULL,          -- sha256 of normalized deck_list
    card_count      INTEGER DEFAULT 0,
    change_summary  TEXT DEFAULT '',        -- "+2 Sheoldred, -2 Cut Down"
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deck_id, version_number)
);

-- Tracks what artifacts exist for each version and their state
CREATE TABLE IF NOT EXISTS deck_version_state (
    version_id          INTEGER PRIMARY KEY REFERENCES deck_versions(version_id),

    -- Rules
    rules_path          TEXT DEFAULT '',        -- relative to USER_RULES_DIR
    rules_source        TEXT DEFAULT '',        -- "mechanical" | "mechanical+llm" | "expert_review"
    rules_count         INTEGER DEFAULT 0,
    rules_validated     INTEGER DEFAULT 0,      -- bool
    rules_issues        TEXT DEFAULT '[]',      -- JSON array from validate_strategy

    -- GA optimization
    ga_status           TEXT DEFAULT 'not_started', -- not_started|queued|running|completed|failed
    ga_fitness          REAL DEFAULT 0,
    ga_matchup_wr       TEXT DEFAULT '{}',      -- JSON: {"Mono Red": 0.65, ...}
    ga_generations      INTEGER DEFAULT 0,
    ga_log_path         TEXT DEFAULT '',

    -- Test run (quick eval, no optimization)
    test_run_status     TEXT DEFAULT 'not_started',
    test_run_wr         REAL DEFAULT 0,
    test_run_results    TEXT DEFAULT '{}',      -- JSON: per-matchup 10-game results

    -- Deployment
    is_deployed         INTEGER DEFAULT 0,      -- bool: is this the live version?

    updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Timeline: every action logged
CREATE TABLE IF NOT EXISTS deck_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id     TEXT NOT NULL REFERENCES decks(deck_id),
    version_id  INTEGER REFERENCES deck_versions(version_id),  -- NULL for deck-level events
    event_type  TEXT NOT NULL,   -- created|version_added|rules_generated|rules_validated|
                                 -- ga_started|ga_completed|ga_failed|test_run|deployed|undeployed|note
    summary     TEXT DEFAULT '',
    details     TEXT DEFAULT '{}',  -- JSON blob with event-specific data
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_deck_events_deck ON deck_events(deck_id);
```

### Why SQLite, not pure JSON?

- Querying history, filtering by status, comparing versions — SQL is natural.
- Already have `advisor.db` with the same connection pattern.
- Strategy JSONs remain the source of truth for the rule engine (no change).
- The SQLite tables are metadata *about* the JSON files, not a replacement.

### Deck ID Convention

`deck_id` = slugified name: `"Rakdos Midrange"` -> `"rakdos_midrange"`.
Same slug used for the `.json` strategy file and `.txt` deck file.
Versioned files: `rakdos_midrange.json` (always latest deployed),
`rakdos_midrange_v2.json` etc. for historical versions.

---

## 2. API Endpoints

All under `/api/decks/`. The existing `/api/manage/*` endpoints stay unchanged.

### CRUD

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/api/decks` | List all decks with current version + state summary |
| `POST` | `/api/decks` | Create deck (name + deck_list text) |
| `GET` | `/api/decks/{deck_id}` | Full deck detail: all versions, current state, timeline |
| `PUT` | `/api/decks/{deck_id}` | Update name/description |
| `DELETE` | `/api/decks/{deck_id}` | Delete deck + all versions |

### Versions

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/api/decks/{deck_id}/versions` | Add new version (new deck_list, auto-diff) |
| `GET` | `/api/decks/{deck_id}/versions/{v}` | Get specific version detail |
| `GET` | `/api/decks/{deck_id}/diff/{v1}/{v2}` | Card-level diff between two versions |

### Workflow Actions

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/api/decks/{deck_id}/versions/{v}/generate-rules` | Generate rules (mechanical, +llm flag) |
| `POST` | `/api/decks/{deck_id}/versions/{v}/validate` | Validate rules |
| `POST` | `/api/decks/{deck_id}/versions/{v}/ga` | Start GA optimization |
| `GET` | `/api/decks/{deck_id}/versions/{v}/ga/status` | Poll GA status |
| `POST` | `/api/decks/{deck_id}/versions/{v}/test-run` | Quick 10-game eval |
| `GET` | `/api/decks/{deck_id}/versions/{v}/test-run/status` | Poll test run status |
| `POST` | `/api/decks/{deck_id}/versions/{v}/deploy` | Deploy this version to live advisor |
| `POST` | `/api/decks/{deck_id}/versions/{v}/undeploy` | Remove from live advisor |

### Import Existing

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/api/decks/import` | Import existing .txt + .json pair into managed lifecycle |

### Timeline

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/api/decks/{deck_id}/timeline` | Full event log |

---

## 3. UI Pages / Components

### Option A (recommended): New tab in manage.html

Add a **"Decks"** tab that replaces the current simple deck viewer. The existing
"Strategies", "General Rules", "Meta Decks", "Guides" tabs stay as-is.

### Deck List View (the tab)

```
┌─────────────────────────────────────────────────────────┐
│  [Rakdos Midrange]  BR  midrange  v5  ●deployed         │
│   42 rules (expert_review) · GA 73% · last: 2d ago     │
│                                                          │
│  [Mono White Lifegain]  W  aggro  v3  ○not deployed     │
│   28 rules (mechanical+llm) · GA 68% · last: 5d ago    │
│                                                          │
│  [+ Create New Deck]                                     │
└─────────────────────────────────────────────────────────┘
```

Status badges on each row:
- Color dots (existing)
- Version number
- Has rules? (check / x)
- GA status (not started / running / complete with %)
- Deployed? (green dot)

### Deck Detail View (click a deck)

Opens as an expanded panel below the list (same pattern as strategy detail).

```
┌─ Rakdos Midrange ──────────────────────────────────────┐
│  BR · midrange · v5 (deployed)                          │
│  Description: [editable text field]                     │
│                                                          │
│  ┌─ Actions ──────────────────────────────────────────┐ │
│  │ [Generate Rules ▾]  [Validate]  [Run GA]           │ │
│  │ [Quick Test Run]    [Deploy v5]  [Compare...]      │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─ Version Timeline ─────────────────────────────────┐ │
│  │ v5  +2 Sheoldred, -2 Cut Down           Mar 20     │ │
│  │     ├ Rules generated (mechanical+llm, 42 rules)   │ │
│  │     ├ GA completed: 73% overall WR                 │ │
│  │     └ Deployed to advisor                          │ │
│  │                                                     │ │
│  │ v4  +1 Hero's Downfall, -1 Duress        Mar 15    │ │
│  │     ├ Rules generated (mechanical, 38 rules)       │ │
│  │     ├ GA completed: 68% overall WR                 │ │
│  │     └ Undeployed (replaced by v5)                  │ │
│  │                                                     │ │
│  │ v1  Initial import                        Mar 1    │ │
│  │     └ Rules generated (mechanical, 35 rules)       │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─ Current Deck List ────────────────────────────────┐ │
│  │ [MTGA format textarea, editable]                   │ │
│  │ [Save as New Version]                              │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─ GA Results (v5) ─────────────────────────────────┐ │
│  │ Overall: 73% · Mono Red: 81% · UW Control: 55%   │ │
│  │ [bar chart per matchup]                            │ │
│  └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

### Create New Deck Modal

Simple form:
- Name (text input)
- Deck list (textarea, paste from MTGA)
- [Create] button
- Auto-detects colors + archetype from card analysis after creation.

---

## 4. Workflow Automation

### Generate Rules

**Local, synchronous** (fast enough for 1 user).

```
POST /api/decks/{deck_id}/versions/{v}/generate-rules
Body: { "mode": "mechanical" }  // or "mechanical+llm"
```

Implementation:
1. Read deck_list from `deck_versions` table.
2. Write to temp file (MTGA format).
3. Call `generate_rules.generate_strategy(temp_path, deck_name)`.
4. If `mode == "mechanical+llm"`, also call `_enrich_with_llm()`.
5. Write result to `USER_RULES_DIR/{deck_id}_v{n}.json`.
6. Update `deck_version_state.rules_path`, `rules_source`, `rules_count`.
7. Log `deck_events` entry.
8. Return the strategy summary.

Reuses: `advisor/generate_rules.py` entirely — `generate_strategy()` and
`_enrich_with_llm()` are already factored out as callable functions.

### Validate Rules

**Local, synchronous.**

```
POST /api/decks/{deck_id}/versions/{v}/validate
```

Calls `validate_strategy.validate_strategy(path, fix=True)`.
Updates `rules_validated`, `rules_issues` in state table.

Reuses: `advisor/validate_strategy.py` — already a standalone function.

### Deploy

**Local, instant.**

```
POST /api/decks/{deck_id}/versions/{v}/deploy
```

1. Copy `{deck_id}_v{n}.json` -> `{deck_id}.json` in USER_RULES_DIR.
2. Copy deck_list -> `{deck_id}.txt` in USER_DATA_DIR/decks/.
3. Set `is_deployed = 1` on this version, `0` on all others.
4. Reload strategy in the running advisor (call `advisor.reload_strategy()`).
5. Log event.

---

## 5. GA Integration

This is the only part that's truly async and remote.

### Architecture: SSH + polling (simple, no infra)

```
User clicks [Run GA]
    │
    ▼
Server writes deck + strategy to temp staging dir
    │
    ▼
Server runs: ssh studio "cd ~/MTG/mtg-simlab && ./run_ga.sh {deck_id} {version}"
  (non-blocking, via asyncio.create_subprocess_exec)
    │
    ▼
Server stores ga_status = "running", PID/job_id
    │
    ▼
UI polls GET /api/decks/{deck_id}/versions/{v}/ga/status every 10s
    │
    ▼
Server checks: ssh studio "cat /tmp/ga_{deck_id}_v{n}/status.json"
  (status.json written by GA script: generation, fitness, matchups)
    │
    ▼
When status.json says "completed":
  - rsync results back: studio:~/MTG/mtg-simlab/output/{deck_id}/ -> local
  - Parse GA log, update deck_version_state
  - Log event
```

### Why not WebSocket/webhook?

- 1 user, 3-5 decks. Polling every 10s is trivially cheap.
- No webhook infra needed on Studio.
- SSH is already the communication channel (rsync, tmux).
- GA runs take 2-8 hours — 10s polling is plenty.

### GA Script Contract

The GA runner on Studio needs a simple contract:

```bash
# Input: strategy JSON + config already rsynced to Studio
# Output: /tmp/ga_{deck_id}_v{n}/status.json with:
{
    "status": "running",           // or "completed" or "failed"
    "generation": 42,
    "best_fitness": 0.73,
    "matchup_wr": {"Mono Red": 0.81, "UW Control": 0.55},
    "elapsed_minutes": 120,
    "error": null
}
# Output: /tmp/ga_{deck_id}_v{n}/result.json — final optimized strategy
```

### Quick Test Run

Same pattern but much faster (~5 min for 10 games x ~8 matchups).

```
POST /api/decks/{deck_id}/versions/{v}/test-run
```

Runs on Studio via SSH: `./run_test.sh {deck_id} {version} --games 10`
Polls until done. Results stored in `test_run_results`.

---

## 6. MVP vs Future

### Phase 1 — MVP (build this first)

**Goal**: Deck is a first-class entity with version history. All existing
CLI workflows accessible from the UI.

1. **SQLite schema** — `decks`, `deck_versions`, `deck_version_state`, `deck_events` tables.
2. **CRUD endpoints** — create deck, add version, list decks with status.
3. **UI deck list + detail** — new tab in manage.html with deck cards, version timeline, deck list editor.
4. **Generate rules** — button calls existing `generate_strategy()`, saves result, logs event.
5. **Validate** — button calls existing `validate_strategy()`.
6. **Deploy** — button copies strategy JSON to active location.
7. **Import existing** — one-time import of current mtg-data decks+strategies into the lifecycle system.
8. **Timeline view** — shows event log per deck.

**Excludes from MVP**: GA integration, test runs, version diff UI, LLM enrichment button.

**Estimated effort**: ~2-3 sessions. Mostly wiring existing code to new endpoints + UI.

### Phase 2 — GA + Test Runs

9. **GA trigger** — SSH to Studio, poll status, rsync results back.
10. **Test run trigger** — same pattern, lighter job.
11. **GA results display** — matchup win-rate bars in deck detail.
12. **Status polling** — UI polls ga/status endpoint while running.

**Estimated effort**: ~2 sessions. The GA Docker setup already exists.

### Phase 3 — Polish

13. **Version diff** — side-by-side card diff between versions.
14. **LLM enrichment button** — "Generate Rules" dropdown with mechanical / +LLM option.
15. **GA comparison** — overlay two versions' matchup WR charts.
16. **Auto-detect version** — when deck_list hash changes, auto-prompt for new version.
17. **Export** — download deck + strategy as zip for sharing.

### Future / Cherry on Top

18. **Test run on card change** — before creating a version, run 10 games to preview impact.
19. **Strategy diff** — show which rules changed between versions.
20. **Collection awareness** — flag cards in deck you don't own, suggest wildcards needed.
21. **Auto-GA on new version** — automatically queue GA when rules are generated.

---

## 7. What Existing Code to Reuse

| Component | Reuse How |
|-----------|-----------|
| `generate_rules.generate_strategy()` | Call directly for rule generation |
| `generate_rules._enrich_with_llm()` | Call for LLM enrichment mode |
| `generate_rules._parse_decklist()` | Parse pasted deck list text |
| `generate_rules._analyze_deck()` | Auto-detect colors, archetype, signature |
| `validate_strategy.validate_strategy()` | Call for validation action |
| `strategy._all_strategy_dirs()` | Know where to read/write strategy files |
| `strategy.USER_RULES_DIR` | Target dir for generated strategies |
| `database.get_connection()` | Same DB, same connection pattern |
| `database.init_db()` | Add new tables in same migration pattern |
| `manage.html` CSS + tab system | Add "Decks" tab using identical patterns |
| `manage.html` toast, detail panel | Reuse existing UI components |
| `server.py` manage endpoints | Same pattern for new `/api/decks/*` |

### New Code Needed

| File | Purpose |
|------|---------|
| `advisor/deck_lifecycle.py` | Data access layer: CRUD for decks, versions, state, events |
| `advisor/server.py` (extend) | New `/api/decks/*` endpoints |
| `static/manage.html` (extend) | New "Decks" tab with lifecycle UI |
| `advisor/database.py` (extend) | Add lifecycle tables to `init_db()` |
| `tools/import_existing_decks.py` | One-time script to import current decks into lifecycle |

### File Organization

```
advisor/
  deck_lifecycle.py          # NEW — all deck lifecycle logic
  database.py                # EXTEND — add tables
  server.py                  # EXTEND — add endpoints
  generate_rules.py          # UNCHANGED — called by lifecycle
  validate_strategy.py       # UNCHANGED — called by lifecycle

static/
  manage.html                # EXTEND — add Decks tab

tools/
  import_existing_decks.py   # NEW — one-time migration
```

---

## 8. Key Design Decisions

### Decision 1: Deck list stored in DB, not just as .txt file
**Why**: Enables version diffing, hash comparison, and independence from file system.
The .txt file is a *deployment artifact* (written on deploy), not the source of truth.

### Decision 2: Strategy JSON files remain the runtime format
**Why**: The rule engine (`strategy.py`) already loads from JSON files. No reason to
change that. The lifecycle layer manages *which* JSON file is active, not how it's read.

### Decision 3: One SQLite DB, not separate per-deck JSON
**Why**: Cross-deck queries ("which decks need GA?"), timeline across all decks,
single backup. Personal tool — no need for distributed storage.

### Decision 4: No WebSocket for GA status, just polling
**Why**: GA runs hours. 10s polling = trivial. Avoids maintaining a persistent
connection to Studio. SSH + rsync is the simplest reliable transport.

### Decision 5: Version numbers, not git-style hashes
**Why**: Human-readable. "v3 had 73% WR" is more useful than "abc123 had 73% WR".
For 3-5 decks with 5-10 versions each, sequential integers are fine.

### Decision 6: Import existing decks as opt-in migration
**Why**: Don't break anything. Existing unmanaged decks keep working. The import
script creates lifecycle entries for decks that already have .txt + .json files.
