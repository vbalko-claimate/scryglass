"""Real-game advisor validation — the EXTERNAL check.

The whole strategic pivot was about not validating the engine against itself.
This reads the advice_compliance + decision_outcome events the advisor already
logs during REAL ladder games and asks: when the player followed the advisor's
pick, did it correlate with better outcomes?

Three signals (honest, possibly confounded — reported as-is):
  * agreement  — how often the player followed the advisor on advised spots
  * local swing — board+life swing a couple turns after followed vs deviated
                  decisions (a per-decision quality signal)
  * win lift   — win-rate of high- vs low-compliance games (whole-game; the
                 weakest signal — deviations cluster in already-decided games)
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict

from .database import get_connection


def advice_validation() -> dict:
    conn = get_connection()
    conn.row_factory = sqlite3.Row

    # Compliance, deduped per decision (some decisions log twice — prefer the
    # event that actually carried advice, rec_count > 0).
    comp: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT match_id, data FROM match_events WHERE event_type='advice_compliance'"
    ):
        d = json.loads(r["data"])
        did = d.get("decision_id")
        if not did:
            continue
        has = d.get("rec_count", 0) > 0
        if did not in comp or (has and not comp[did]["has"]):
            comp[did] = {"match": r["match_id"], "has": has, "followed": bool(d.get("followed"))}

    advised = [v for v in comp.values() if v["has"]]
    followed = sum(1 for v in advised if v["followed"])

    # Local outcome (board+life swing) per decision, joined back by decision_id.
    swing: dict[str, float] = {}
    for r in conn.execute(
        "SELECT data FROM match_events WHERE event_type='decision_outcome'"
    ):
        d = json.loads(r["data"])
        did = d.get("decision_id")
        if did:
            swing[did] = (d.get("life_delta", 0) - d.get("opp_life_delta", 0)) + (
                d.get("creature_delta", 0) - d.get("opp_creature_delta", 0)
            )
    fol = [swing[did] for did, v in comp.items() if v["has"] and v["followed"] and did in swing]
    nfo = [swing[did] for did, v in comp.items() if v["has"] and not v["followed"] and did in swing]

    # Per-game compliance rate vs result (whole-game, weakest signal).
    results = {
        r["match_id"]: r["result"]
        for r in conn.execute("SELECT match_id, result FROM matches WHERE result != ''")
    }
    conn.close()

    per_match: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [advised, followed]
    for v in advised:
        per_match[v["match"]][0] += 1
        per_match[v["match"]][1] += 1 if v["followed"] else 0
    rates = sorted(
        (f / tot, results[m].startswith("Win"))
        for m, (tot, f) in per_match.items()
        if m in results and tot >= 3
    )
    half = len(rates) // 2

    def wr(rs: list) -> float | None:
        return round(100 * sum(w for _, w in rs) / len(rs), 1) if rs else None

    return {
        "decisions_with_advice": len(advised),
        "followed": followed,
        "agreement_pct": round(100 * followed / max(1, len(advised)), 1),
        "local_swing_followed": round(statistics.mean(fol), 2) if fol else None,
        "local_swing_not_followed": round(statistics.mean(nfo), 2) if nfo else None,
        "n_followed": len(fol),
        "n_not_followed": len(nfo),
        "games_evaluated": len(rates),
        "win_high_compliance": wr(rates[half:]),
        "win_low_compliance": wr(rates[:half]),
        "note": (
            "Real-game external validation. 'Local swing' (followed vs deviated) "
            "is the per-decision quality signal; whole-game win-by-compliance is "
            "confounded (deviations cluster in already-decided games)."
        ),
    }
