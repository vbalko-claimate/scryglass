# ADR: Deck Lifecycle Architecture

**Date**: 2026-03-23
**Status**: Proposed
**Context**: Scryglass needs deck lifecycle management. Current state: 1 user, ~5 active decks, ~35 strategy files, vanilla JS frontend. Future: open-source release or SaaS platform with multiple users.

---

## 1. Architecture Decision Record

### Tension 1: Build for scale now vs YAGNI

**Decision: Build the RIGHT abstractions now, defer the INFRASTRUCTURE.**

The data model and API surface should be designed so they don't need rewriting when user #2 arrives. But we don't build auth, multi-tenancy, billing, or horizontal scaling until there's a real need.

Concretely: `deck_lifecycle.py` as a clean module with a `DeckService` class that takes a `user_id` parameter (hardcoded to `"local"` for now). Every query includes `user_id` in the WHERE clause even though there's only one. Cost: ~0 extra effort. Payoff: multi-user is a database migration, not a rewrite.

### Tension 2: SQLite vs files

**Decision: SQLite for metadata + state, JSON files remain source of truth for rules.**

Agent A is right that SQLite is the correct choice for structured metadata (versions, state, events). Agent B is right that for 3 decks it's overkill -- but we ALREADY have `advisor.db` with the same pattern. Adding 2-3 tables to an existing SQLite database is zero additional complexity.

JSON strategy files remain the runtime format. The rule engine loads them. The lifecycle layer tracks which file is active and why, it doesn't replace the files.

Key: strategy JSONs are user-portable artifacts. SQLite is internal bookkeeping.

### Tension 3: API design

**Decision: Clean RESTful API, but NOT 15+ endpoints on day 1.**

Agent A's 15+ endpoints include things like `/diff/{v1}/{v2}`, separate GA status polling, test run status polling -- features that don't exist yet. Design the URL scheme for the full API (so nothing needs renaming later), but only implement what has a UI consumer today.

The URL scheme is right: `/api/decks/{deck_id}/versions/{v}/...`. Keep it. But Phase 1 implements 7 endpoints, not 15.

### Tension 4: Version system

**Decision: Keep versions, but simplify to 2 tables instead of 3.**

Agent B's "active + previous" is too simplistic for a platform where users will want to compare GA results across iterations. But Agent A's separate `deck_version_state` table adds a join for every query with minimal benefit -- the state IS the version's state. Merge `deck_versions` and `deck_version_state` into one `deck_versions` table.

Version numbers (v1, v2, v3) not hashes. Humans read these.

### Tension 5: Separation of concerns

**Decision: `deck_lifecycle.py` as a standalone module, NOT woven into server.py.**

Agent B's concern about manage.html at 1072 lines is valid. The answer is NOT to avoid building the feature -- it's to structure it properly:
- `advisor/deck_lifecycle.py` -- pure business logic, no HTTP concerns
- `advisor/deck_routes.py` -- FastAPI router (APIRouter), mounted into server.py with 1 line
- `static/decks.html` -- NEW page, not crammed into manage.html

This keeps server.py from growing and keeps manage.html as-is. The Decks page is a separate concern with its own complexity budget.

### Tension 6: Data portability

**Decision: Export = zip of (decklist.txt + strategy.json + metadata.json). Import = reverse.**

The JSON strategy files are already portable. Add a thin metadata envelope on export. SQLite is NOT the export format -- it's internal bookkeeping that gets regenerated on import.

---

## 2. Revised Data Model

### 2 tables, not 4. Merge version+state. Drop events table for now.

```sql
-- Core deck identity
CREATE TABLE IF NOT EXISTS decks (
    deck_id     TEXT PRIMARY KEY,              -- slug: "rakdos_midrange"
    user_id     TEXT NOT NULL DEFAULT 'local', -- future multi-tenancy
    name        TEXT NOT NULL,                 -- "Rakdos Midrange"
    description TEXT DEFAULT '',
    colors      TEXT DEFAULT '[]',             -- JSON array: ["B","R"]
    archetype   TEXT DEFAULT '',               -- "midrange", "aggro", etc.
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_decks_user ON decks(user_id);

-- Version = decklist snapshot + all its state in one row
CREATE TABLE IF NOT EXISTS deck_versions (
    version_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    deck_id         TEXT NOT NULL REFERENCES decks(deck_id) ON DELETE CASCADE,
    version_number  INTEGER NOT NULL,          -- 1, 2, 3...
    deck_list       TEXT NOT NULL,             -- full MTGA import text
    deck_list_hash  TEXT NOT NULL,             -- sha256 of normalized list
    card_count      INTEGER DEFAULT 0,
    change_summary  TEXT DEFAULT '',           -- "+2 Sheoldred, -2 Cut Down"

    -- Rules state (merged from deck_version_state)
    rules_path      TEXT DEFAULT '',           -- relative to USER_RULES_DIR
    rules_source    TEXT DEFAULT '',           -- "mechanical" | "mechanical+llm" | "expert_review"
    rules_count     INTEGER DEFAULT 0,
    rules_validated INTEGER DEFAULT 0,

    -- GA state (Phase 2, columns exist but unused in Phase 1)
    ga_status       TEXT DEFAULT 'not_started',
    ga_fitness      REAL DEFAULT 0,
    ga_generations  INTEGER DEFAULT 0,

    -- Deployment
    is_active       INTEGER DEFAULT 0,        -- is this the live version?

    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(deck_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_versions_deck ON deck_versions(deck_id);
```

### What was cut and why

| Original table | Decision | Reason |
|---|---|---|
| `deck_version_state` | **Merged into `deck_versions`** | Always 1:1 with version. Extra join adds query complexity for no benefit. |
| `deck_events` | **Deferred to Phase 2** | For 1 user, the version history IS the timeline. Events become valuable when there are multiple actors (GA bot, other users, automation). Add the table when building GA integration -- it's a pure addition, no migration needed. |

### What was kept from Agent A's design

- `deck_id` as slug (correct for URL routing)
- `deck_list` stored in DB (enables diffing without filesystem)
- `version_number` as integer (human-readable)
- `is_active` flag (single source of truth for "which version is live")
- All GA columns (they're just NULL/default -- zero cost to have them ready)

### What Agent B got right (incorporated)

- **Strategy inheritance is unaddressed in Agent A's design.** Added to Phase 1 requirements below: when creating a new version, the previous version's rules are COPIED as a starting point, not regenerated from scratch. Hand-tuned expert rules must survive deck updates.
- **Collection awareness** belongs early. Not in the data model (it's a runtime check), but in the Phase 1 API response: flag cards in deck that aren't in collection.

---

## 3. Revised API

### Phase 1 -- MVP (7 endpoints)

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/api/decks` | List all decks with active version summary |
| `POST` | `/api/decks` | Create deck (name + decklist text) |
| `GET` | `/api/decks/{deck_id}` | Deck detail: all versions, active version state |
| `DELETE` | `/api/decks/{deck_id}` | Delete deck + all versions |
| `POST` | `/api/decks/{deck_id}/versions` | Add new version (new decklist, auto-diff, inherit rules) |
| `POST` | `/api/decks/{deck_id}/versions/{v}/generate-rules` | Generate rules (mode: mechanical / +llm) |
| `POST` | `/api/decks/{deck_id}/versions/{v}/deploy` | Deploy version: copy to active, reload advisor |

Notes:
- `PUT /api/decks/{deck_id}` (update name/description) -- trivial, add if needed, but not required for MVP flow.
- Validation happens inside `generate-rules` automatically (always validate after generating). No separate endpoint needed.
- Collection check is embedded in the `GET /api/decks/{deck_id}` response, not a separate endpoint.

### Phase 2 -- GA + Automation (add 4 endpoints)

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/api/decks/{deck_id}/versions/{v}/ga` | Start GA optimization |
| `GET` | `/api/decks/{deck_id}/versions/{v}/ga/status` | Poll GA status |
| `POST` | `/api/decks/{deck_id}/versions/{v}/test-run` | Quick evaluation run |
| `GET` | `/api/decks/{deck_id}/timeline` | Event log (add `deck_events` table here) |

### Phase 3 -- Platform (add when needed)

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/api/decks/{deck_id}/diff/{v1}/{v2}` | Card-level diff |
| `POST` | `/api/decks/import` | Import from file upload |
| `GET` | `/api/decks/{deck_id}/export` | Export deck + rules as zip |
| `POST` | `/api/decks/{deck_id}/versions/{v}/expert-review` | Trigger multi-model review |

### API design principles for future compatibility

1. All responses include `deck_id` and `version_number` -- clients never need to construct these.
2. List endpoints return summaries; detail endpoints return full objects. No N+1 calls needed.
3. Action endpoints (generate-rules, deploy, ga) return the updated version object, not just "ok".
4. Error responses always: `{"error": "message", "code": "MACHINE_READABLE_CODE"}`.

---

## 4. Implementation Plan

### Phase 1: Works for 1 user, right abstractions for N users
**Effort: 2 sessions**

#### 1a. Data layer (`advisor/deck_lifecycle.py`)
- `DeckService` class with `user_id` parameter
- Methods: `create_deck()`, `list_decks()`, `get_deck()`, `delete_deck()`, `add_version()`, `generate_rules()`, `deploy_version()`
- `add_version()` auto-copies previous version's rules (strategy inheritance!)
- `generate_rules()` calls existing `generate_strategy()` + `validate_strategy()`
- `deploy_version()` copies JSON to active location + calls `advisor.reload_strategy()`
- Pure Python, no HTTP concerns. Testable independently.

#### 1b. Routes (`advisor/deck_routes.py`)
- FastAPI `APIRouter` with prefix `/api/decks`
- 7 endpoints, thin wrappers around `DeckService`
- Mount into `server.py` with: `app.include_router(deck_router)`

#### 1c. Schema migration (`advisor/database.py`)
- Add `decks` and `deck_versions` tables to `init_db()`
- Same pattern as existing card/match tables

#### 1d. UI (`static/decks.html`)
- NEW page, not a new tab in manage.html (keeps manage.html at 1072 lines)
- Link from manage.html: "Manage Decks ->" link/button
- Deck list with status badges
- Deck detail: version list, decklist editor, action buttons
- Create deck modal (paste from MTGA)
- Vanilla JS (matches existing stack). No framework.

#### 1e. Import script (`tools/import_existing_decks.py`)
- One-time: scan `mtg-data/decks/*.txt` + `mtg-data/strategies/*.json`
- Match pairs by name slug
- Create deck + v1 entries in lifecycle tables
- Non-destructive: doesn't move or delete any files

#### 1f. Collection awareness
- In `GET /api/decks/{deck_id}` response, include `missing_cards: [...]` by comparing decklist against loaded collection
- Reuses existing collection data from `database.py`

### Phase 2: GA + multi-model review
**Effort: 2-3 sessions. Build ONLY when GA Docker is stable and tested.**

- Add `deck_events` table (now it's useful: GA start/stop/fail events)
- GA trigger/poll endpoints
- Test run endpoints
- Timeline UI in deck detail
- SSH + polling architecture per Agent A's design (correct for this use case)

### Phase 3: Platform features
**Effort: when preparing for open-source or SaaS launch**

- `user_id` becomes real (auth system)
- Export/import endpoints
- Deck sharing (public URLs)
- Version diff UI
- React/Svelte frontend (replace vanilla JS)

---

## 5. What to EXPLICITLY defer

| Feature | Defer until | Why |
|---|---|---|
| `deck_events` table / timeline | Phase 2 (GA) | For 1 user, version history IS the timeline. Events add value when there are automated actors. |
| Event timeline UI | Phase 2 | No events = no timeline to show. |
| Version diff endpoint + UI | Phase 3 | Nice to have, not needed for workflow. User can eyeball 60-card lists. |
| WebSocket for GA status | Forever (or until 100+ concurrent users) | Polling every 10s is fine for years. WebSocket adds complexity with zero UX benefit for async jobs that take hours. |
| `test_run_*` columns and endpoints | Phase 2 | Quick eval is a GA-adjacent feature. Build together. |
| Separate deck `PUT` endpoint | Phase 1 late / Phase 2 | Renaming a deck is rare. If needed, add in a day. |
| Auto-detect version on hash change | Phase 3 | Over-engineering. User explicitly creates versions. |
| LLM enrichment as separate button | Phase 2 | `generate-rules` with `mode: "mechanical+llm"` flag is enough. |
| Multi-user auth | Phase 3 | Zero users besides you. `user_id='local'` placeholder is sufficient. |
| Billing / subscription | Phase 3+ | No product-market fit yet. |
| React/mobile frontend | Phase 3+ | Vanilla JS serves 1-10 users fine. The clean REST API means any frontend can be built later without backend changes. |

---

## 6. Key Architectural Bets

### Bet 1: Separate `decks.html` over extending `manage.html`
manage.html is 1072 lines and growing. Deck lifecycle is a fundamentally different workflow (create -> version -> optimize -> deploy) from strategy editing (read -> tweak rules -> save). A new page with its own JS keeps both maintainable. Link between them.

### Bet 2: `DeckService` class over loose functions
A class with `user_id` in the constructor makes multi-tenancy a constructor parameter change, not a function signature audit across 15 call sites. For Phase 1, `DeckService("local")` everywhere.

### Bet 3: Strategy inheritance by default
When `add_version()` is called, the previous version's strategy JSON is copied to the new version's path. The user can then regenerate if they want, but hand-tuned expert rules are preserved by default. This is the single most important UX decision -- losing hours of expert-reviewed rules on a 2-card sideboard swap would be catastrophic.

### Bet 4: GA columns exist but are unused
Adding `ga_status`, `ga_fitness`, `ga_generations` columns to `deck_versions` now costs nothing (they default to null/zero) but avoids a schema migration when Phase 2 arrives. This is not premature optimization -- it's avoiding a guaranteed future migration for zero present cost.

### Bet 5: No ORM
Direct SQLite with `sqlite3` module, same as the rest of the codebase. For 2 tables with simple queries, an ORM adds dependency and complexity. When/if we move to PostgreSQL for SaaS, SQLAlchemy can be introduced then.

---

## Summary: What Each Agent Got Right

**Agent A (Architect) was right about:**
- SQLite over pure files (we already have the pattern)
- Deck as first-class entity with identity across versions
- RESTful URL scheme (`/api/decks/{deck_id}/versions/{v}/...`)
- Version numbers over hashes
- Storing decklist in DB (not just filesystem)
- Deploy as an explicit action (copy to active location + reload)

**Agent B (Critic) was right about:**
- 4 tables is 1-2 too many for Phase 1
- Event timeline is useless for 1 user (defer to Phase 2)
- manage.html shouldn't grow (new page instead)
- Strategy inheritance was unaddressed (now it's a core requirement)
- Collection awareness belongs early
- GA failure modes need explicit handling (error states in schema)

**Neither agent addressed:**
- `user_id` placeholder for future multi-tenancy (nearly free, massive future payoff)
- Separating routes into `deck_routes.py` (FastAPI Router pattern)
- The import script as a first-class deliverable (critical for adoption of the new system)
