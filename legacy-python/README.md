# legacy-python — parked Python host (NOT built, NOT shipped)

This directory holds the **retired Python host** for Scryglass. It is kept for
reference only. Nothing here is part of the product anymore.

## Status

- **Not delivered.** Nothing under this directory is bundled by Tauri
  (`src-tauri/tauri.conf.json` `externalBin`/`resources` do not reference it).
- **Not built.** CI (`.github/workflows/release.yml`) and
  `tools/build_and_test.sh` no longer invoke PyInstaller or any of this code.
- **Not used at runtime.** The Tauri shell spawns the all-Rust `glass-host`
  (crate `glass-mtga`) as the sole backend; there is no Python fallback
  (`src-tauri/src/sidecar.rs`).

## What lives here

| Path | Was |
|------|-----|
| `advisor/` | The Python advisor (heuristics + 7-layer rule DSL + LLM). ~21k LOC. |
| `run.py` | Uvicorn entry point for the Python host. |
| `scry-server.spec` | PyInstaller spec that produced the `scry-server` sidecar. |
| `pyproject.toml`, `uv.lock` | Python dependency manifests. |
| `tools/` | Python dev/maintenance utilities (`backup_db.py`, `export_replay.py`, `update_meta.py`, `mtga_reader/`, etc.). |

## The replacement

The 1:1 Rust port lives in the sibling repo **glass-shard**, crate
`glass-mtga` (`src/{recon,server,db,deck_id,manage,state,strategy_store,log_parser,log_watcher,advise_client,ui,llm}.rs`).
It serves the identical HTTP/WS surface on `:8765`. See
`../glass-shard/crates/glass-mtga/CUTOVER.md` for the migration notes.

## To run the old Python host (reference only)

```sh
cd legacy-python
uv sync                 # restore the venv (regenerates .venv)
SCRY_PORT=8766 uv run python run.py   # spare port, never the live :8765
```

Do NOT point the live Tauri app at it — the product is all-Rust now.
