"""Logistic regression reranker for candidate actions.

Learns from telemetry (chosen/not-chosen labels) to re-score engine candidates.
Pure numpy — no sklearn or torch dependency.

Usage:
    uv run python -m advisor.reranker train [--data PATH] [--output PATH]
    uv run python -m advisor.reranker predict --state '{}' --candidates '[{},{}]'
"""
from __future__ import annotations

import argparse, json
from collections import OrderedDict
from pathlib import Path

import numpy as np

from .models import ActionFamily

ACTION_FAMILIES = [f.value for f in ActionFamily]
N_FEATURES = 16
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DATA = DATA_DIR / "training" / "reranker_v1.jsonl"
DEFAULT_MODEL = DATA_DIR / "models" / "reranker_v1.npz"


def build_state_dict(
    turn: int, phase: str, my_life: int, opp_life: int,
    hand_size: int, board_creature_count: int, opp_creature_count: int,
    mana_available: int,
) -> dict:
    """Build normalized state dict for reranker features."""
    return {
        "turn": turn, "phase": phase,
        "my_life": my_life, "opp_life": opp_life,
        "hand_size": hand_size, "board_creature_count": board_creature_count,
        "opp_creature_count": opp_creature_count, "mana_available": mana_available,
    }


def build_mini_ctx_from_state(state) -> dict:
    """Build mini_ctx dict from a GameState object. Single source of truth."""
    me = state.my_player()
    opp = state.opp_player()
    return build_state_dict(
        turn=state.turn_info.turn_number,
        phase=state.turn_info.phase,
        my_life=me.life_total if me else 20,
        opp_life=opp.life_total if opp else 20,
        hand_size=len(state.my_hand()),
        board_creature_count=len(state.my_creatures()),
        opp_creature_count=len(state.opp_creatures()),
        mana_available=len(state.my_untapped_lands()),
    )


def extract_features(state: dict, candidate: dict, n_candidates: int) -> list[float]:
    """Extract 16-dim feature vector for one candidate."""
    # State features (7)
    feats = [
        min(state.get("my_life", 20), 40) / 20.0,
        min(state.get("opp_life", 20), 40) / 20.0,
        min(state.get("hand_size", 0), 14) / 7.0,
        min(state.get("board_creature_count", 0), 10) / 5.0,
        min(state.get("opp_creature_count", 0), 10) / 5.0,
        min(state.get("mana_available", 0), 20) / 10.0,
        min(state.get("turn", 0), 30) / 15.0,
    ]
    # Candidate features (2)
    rank = candidate.get("rank", 0)
    score = candidate.get("score", 0.0)
    feats.append(min(rank, 5) / 5.0)
    feats.append(float(score))
    # Action family one-hot (6)
    af = candidate.get("action_family", "")
    for fam in ACTION_FAMILIES:
        feats.append(1.0 if af == fam else 0.0)
    # Score shape (1): margin only for top candidate
    feats.append((score - 0.5) if rank == 0 else 0.0)
    return feats


def _sigmoid(z: np.ndarray) -> np.ndarray:
    z = np.clip(z, -500, 500)
    return 1.0 / (1.0 + np.exp(-z))


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _group_by_decision(rows: list[dict]) -> OrderedDict[str, list[dict]]:
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for r in rows:
        did = r["decision_id"]
        groups.setdefault(did, []).append(r)
    return groups


def _split_by_match(rows: list[dict], train_frac: float = 0.8):
    """Split rows by match_id (temporal order). Returns train, test rows."""
    seen = []
    seen_set = set()
    for r in rows:
        mid = r["decision_id"].rsplit("_", 2)[0]
        if mid not in seen_set:
            seen.append(mid)
            seen_set.add(mid)
    cutoff = max(1, int(len(seen) * train_frac))
    train_ids = set(seen[:cutoff])
    train = [r for r in rows if r["decision_id"].rsplit("_", 2)[0] in train_ids]
    test = [r for r in rows if r["decision_id"].rsplit("_", 2)[0] not in train_ids]
    return train, test


def _build_matrices(rows: list[dict]):
    """Build X, y matrices from rows."""
    groups = _group_by_decision(rows)
    X_list, y_list = [], []
    for did, candidates in groups.items():
        n = len(candidates)
        for c in candidates:
            feats = extract_features(c["state"], c["candidate"], n)
            X_list.append(feats)
            y_list.append(1.0 if c.get("chosen") else 0.0)
    return np.array(X_list, dtype=np.float64), np.array(y_list, dtype=np.float64)


class Reranker:
    def __init__(self):
        self.weights: np.ndarray | None = None
        self.bias: float = 0.0
        self.trained: bool = False

    def fit(self, X: np.ndarray, y: np.ndarray, lr: float = 0.1,
            epochs: int = 100, reg: float = 0.01) -> None:
        """Fit on feature matrix X and label vector y."""
        n_samples, n_feats = X.shape
        self.weights = np.zeros(n_feats, dtype=np.float64)
        self.bias = 0.0
        for _ in range(epochs):
            z = X @ self.weights + self.bias
            preds = _sigmoid(z)
            error = preds - y
            self.weights -= lr * ((X.T @ error) / n_samples + reg * self.weights)
            self.bias -= lr * error.mean()
        self.trained = True

    def train(self, data_path: Path, lr: float = 0.1, epochs: int = 100,
              reg: float = 0.01) -> dict:
        """Train on JSONL, return metrics dict."""
        rows = _load_jsonl(data_path)
        train_rows, test_rows = _split_by_match(rows)
        X, y = _build_matrices(train_rows)
        if len(X) == 0:
            print("No training data."); return {}

        self.fit(X, y, lr=lr, epochs=epochs, reg=reg)
        # Compute metrics
        metrics = {"train_samples": len(X), "test_samples": 0}
        train_preds = _sigmoid(X @ self.weights + self.bias)
        metrics["train_loss"] = float(-np.mean(
            y * np.log(train_preds + 1e-12) + (1 - y) * np.log(1 - train_preds + 1e-12)))

        if test_rows:
            Xt, yt = _build_matrices(test_rows)
            if len(Xt) > 0:
                test_preds = _sigmoid(Xt @ self.weights + self.bias)
                metrics["test_samples"] = len(Xt)
                metrics["test_loss"] = float(-np.mean(
                    yt * np.log(test_preds + 1e-12) + (1 - yt) * np.log(1 - test_preds + 1e-12)))
        return metrics

    def predict(self, features: list[float]) -> float:
        """Return probability of being chosen."""
        if not self.trained or self.weights is None:
            return 0.5
        x = np.array(features, dtype=np.float64)
        return float(_sigmoid(x @ self.weights + self.bias))

    def rerank(self, state: dict, candidates: list[dict]) -> list[dict]:
        """Rerank candidates by predicted probability. Returns new list."""
        scored = []
        n = len(candidates)
        for c in candidates:
            feats = extract_features(state, c, n)
            prob = self.predict(feats)
            out = dict(c)
            out["reranker_prob"] = prob
            scored.append(out)
        scored.sort(key=lambda x: x["reranker_prob"], reverse=True)
        return scored

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(str(path), weights=self.weights, bias=np.array([self.bias]),
                 trained=np.array([1]))

    def load(self, path: Path):
        data = np.load(str(path))
        self.weights = data["weights"]
        self.bias = float(data["bias"][0])
        self.trained = bool(data["trained"][0])


def main():
    parser = argparse.ArgumentParser(description="Logistic reranker for candidate actions")
    sub = parser.add_subparsers(dest="cmd")

    tr = sub.add_parser("train", help="Train reranker on JSONL data")
    tr.add_argument("--data", type=Path, default=DEFAULT_DATA)
    tr.add_argument("--output", type=Path, default=DEFAULT_MODEL)

    pr = sub.add_parser("predict", help="Predict on state + candidates")
    pr.add_argument("--state", required=True, help="JSON state dict")
    pr.add_argument("--candidates", required=True, help="JSON candidate list")
    pr.add_argument("--model", type=Path, default=DEFAULT_MODEL)

    args = parser.parse_args()
    if args.cmd == "train":
        rr = Reranker()
        metrics = rr.train(args.data)
        if metrics:
            rr.save(args.output)
            print(f"Model saved to {args.output}")
            for k, v in metrics.items():
                print(f"  {k}: {v}")
    elif args.cmd == "predict":
        rr = Reranker()
        rr.load(args.model)
        state = json.loads(args.state)
        candidates = json.loads(args.candidates)
        result = rr.rerank(state, candidates)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
