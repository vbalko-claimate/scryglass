"""Glass Shard simulation client — runs 'what if' analysis via the Rust engine."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GLASS_SHARD_URL = os.environ.get("GLASS_SHARD_URL", "http://localhost:3333")


@dataclass(slots=True)
class SimResult:
    """Result of a Glass Shard matchup simulation."""
    deck_a: str
    deck_b: str
    games: int
    wins_a: int
    wins_b: int
    draws: int
    win_rate_a: float
    win_rate_b: float
    avg_turns: float
    anomalies: int
    throughput: float
    wall_time_ms: int


class GlassShardClient:
    """Client for the Glass Shard HTTP simulation server."""

    def __init__(self, base_url: str | None = None, timeout: float = 120.0):
        self.base_url = base_url or GLASS_SHARD_URL
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def is_available(self) -> bool:
        """Check if Glass Shard server is running."""
        try:
            r = self._client.get("/health")
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False

    def health(self) -> dict:
        """Get server health info."""
        r = self._client.get("/health")
        r.raise_for_status()
        return r.json()

    def simulate(
        self,
        deck_a: str,
        deck_b: str,
        games: int = 1000,
        seed: int = 42,
        ai: str = "heuristic",
    ) -> SimResult:
        """Run a matchup simulation.

        Args:
            deck_a: Deck filename (in server's decks dir) or inline Arena-format text.
            deck_b: Deck filename or inline Arena-format text.
            games: Number of games to simulate.
            seed: RNG seed for reproducibility.
            ai: AI type ("heuristic" or "random").

        Returns:
            SimResult with win rates, anomalies, throughput.
        """
        payload = {
            "deck_a": deck_a,
            "deck_b": deck_b,
            "games": games,
            "seed": seed,
            "ai": ai,
        }
        r = self._client.post("/simulate", json=payload)
        r.raise_for_status()
        data = r.json()

        return SimResult(
            deck_a=data["deck_a"],
            deck_b=data["deck_b"],
            games=data["games"],
            wins_a=data["wins_a"],
            wins_b=data["wins_b"],
            draws=data["draws"],
            win_rate_a=data["win_rate_a"],
            win_rate_b=data["win_rate_b"],
            avg_turns=data["avg_turns"],
            anomalies=data["anomalies"],
            throughput=data["throughput"],
            wall_time_ms=data["wall_time_ms"],
        )

    def simulate_matchup_matrix(
        self,
        decks: list[str],
        games_per_matchup: int = 1000,
    ) -> dict[tuple[str, str], SimResult]:
        """Run all pairwise matchups between a list of decks.

        Returns:
            Dict mapping (deck_a, deck_b) → SimResult.
        """
        results = {}
        for i, a in enumerate(decks):
            for b in decks[i + 1:]:
                logger.info(f"Simulating {a} vs {b} ({games_per_matchup} games)...")
                result = self.simulate(a, b, games=games_per_matchup)
                results[(a, b)] = result
                logger.info(
                    f"  {result.deck_a} {result.win_rate_a:.1f}% vs "
                    f"{result.deck_b} {result.win_rate_b:.1f}% "
                    f"({result.wall_time_ms}ms)"
                )
        return results

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
