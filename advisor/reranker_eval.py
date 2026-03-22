"""Evaluate reranker against engine baseline.

Compares engine top-1 vs reranker top-1 against ground truth (chosen label).

Usage:
    uv run python -m advisor.reranker_eval [--data PATH] [--model PATH]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .reranker import (
    Reranker, _load_jsonl, _split_by_match, _group_by_decision,
    DEFAULT_DATA, DEFAULT_MODEL,
)


def evaluate(data_path: Path, model_path: Path | None = None) -> None:
    rows = _load_jsonl(data_path)
    if not rows:
        print("No data found."); return

    train_rows, test_rows = _split_by_match(rows)
    if not test_rows:
        print("WARNING: Not enough data for test split. All data used for training.")
        print(f"  Total rows: {len(rows)}")
        return

    # Train fresh model on train split only
    rr = Reranker()
    from .reranker import _build_matrices
    X, y = _build_matrices(train_rows)
    if len(X) == 0:
        print("No training samples."); return

    rr.fit(X, y)

    # Evaluate on test decisions
    test_groups = _group_by_decision(test_rows)
    engine_correct = 0
    reranker_correct = 0
    agreed = 0
    evaluated = 0

    for did, candidates in test_groups.items():
        # Find ground truth (chosen=true)
        chosen_ids = [c["candidate"]["rule_id"] for c in candidates if c.get("chosen")]
        if not chosen_ids:
            continue  # can't evaluate without label
        ground_truth = chosen_ids[0]
        evaluated += 1

        # Engine top-1 = rank 0
        sorted_by_rank = sorted(candidates, key=lambda c: c["candidate"].get("rank", 99))
        engine_top = sorted_by_rank[0]["candidate"]["rule_id"]

        # Reranker top-1
        state = candidates[0]["state"]
        cands = [c["candidate"] for c in candidates]
        reranked = rr.rerank(state, cands)
        reranker_top = reranked[0]["rule_id"]

        if engine_top == ground_truth:
            engine_correct += 1
        if reranker_top == ground_truth:
            reranker_correct += 1
        if engine_top == reranker_top:
            agreed += 1

    if evaluated == 0:
        print("WARNING: No test decisions with chosen labels. Cannot evaluate.")
        print(f"  Test decisions: {len(test_groups)}")
        return

    engine_acc = engine_correct / evaluated * 100
    reranker_acc = reranker_correct / evaluated * 100
    lift = reranker_acc - engine_acc
    agreement = agreed / evaluated * 100

    print(f"Reranker Evaluation Report")
    print(f"{'=' * 40}")
    print(f"  Train matches:      {len(set(r['decision_id'].rsplit('_', 2)[0] for r in train_rows))}")
    print(f"  Test matches:       {len(set(r['decision_id'].rsplit('_', 2)[0] for r in test_rows))}")
    print(f"  Test decisions:     {evaluated} (with labels)")
    print(f"  Engine accuracy:    {engine_acc:.1f}%")
    print(f"  Reranker accuracy:  {reranker_acc:.1f}%")
    print(f"  Lift:               {lift:+.1f}%")
    print(f"  Agreement:          {agreement:.1f}%")
    if evaluated < 10:
        print(f"\n  WARNING: Only {evaluated} labeled decisions — results may be noisy.")


def main():
    p = argparse.ArgumentParser(description="Evaluate reranker vs engine baseline")
    p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL,
                   help="(unused — trains fresh on train split)")
    a = p.parse_args()
    evaluate(a.data)


if __name__ == "__main__":
    main()
