"""Business-disjoint evaluation of PA abstention thresholds (TRAIN labels only).

This is a policy-validation utility, NOT test-set training. It consumes the
raw candidate dump produced by run.py --dump or eval_templates.py --out.
No question IDs or business IDs are used by the actual inference policy.

Example:
  python scripts/sweep_policy.py runs/train_dump.jsonl train/answers.jsonl

Select the threshold using TRAIN business-held-out folds; never optimize on
public_test submissions or leaderboard labels.
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter

from sklearn.model_selection import GroupKFold

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamate.scorer import score_one  # noqa: E402

TYPES = {"number", "percent", "string", "date", "boolean", "list", "table"}
GRID = [round(i * 0.05, 2) for i in range(20)]


def read_jsonl(path):
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def apply_policy(row, strength: float):
    """Only weak, template-only typed results are filtered. UA evidence remains."""
    pred = row.get("pred") or {"type": "abstain"}
    f = row.get("feats") or {}
    if (pred.get("type") in TYPES and not f.get("llm_used")
            and f.get("tpl_agree")
            and float(f.get("tpl_strength", 0.0)) < strength):
        return {"type": "abstain"}
    return pred


def pa(rows, golds, threshold):
    if not rows:
        return 0.0
    return sum(score_one(apply_policy(row, threshold), golds[row["id"]]) for row in rows) / len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="run.py --dump or eval_templates.py --out, from TRAIN")
    ap.add_argument("gold", help="TRAIN answers.jsonl")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--out", help="optional report JSON path")
    a = ap.parse_args()
    golds = {x["id"]: x for x in read_jsonl(a.gold)}
    rows = [x for x in read_jsonl(a.dump) if x.get("id") in golds]
    if not rows:
        raise SystemExit("No labeled train questions matched; check the input paths")
    groups = [str(x.get("business_id") or "") for x in rows]
    if not all(groups) or len(set(groups)) < 2:
        raise SystemExit("At least two identifiable train businesses are required to avoid question-level leakage")
    nfold = min(max(2, a.folds), len(set(groups)))
    folds = GroupKFold(n_splits=nfold)
    fold_reports = []
    total = 0
    n = 0
    for idx_train, idx_test in folds.split(rows, groups=groups):
        train = [rows[i] for i in idx_train]
        test = [rows[i] for i in idx_test]
        # Resolve equal training PA in favour of more abstention (higher threshold).
        chosen = max(GRID, key=lambda t: (pa(train, golds, t), t))
        held = pa(test, golds, chosen)
        baseline = pa(test, golds, 0.0)
        default = pa(test, golds, 0.45)
        fold_reports.append({"n_train": len(train), "n_heldout": len(test),
                             "heldout_businesses": len(set(groups[i] for i in idx_test)),
                             "chosen_train_threshold": chosen,
                             "heldout_PA": round(held, 6), "raw_PA": round(baseline, 6),
                             "default_0_45_PA": round(default, 6)})
        total += held * len(test)
        n += len(test)
    recommended = statistics.median(x["chosen_train_threshold"] for x in fold_reports)
    result = {"n_questions": len(rows), "n_businesses": len(set(groups)),
              "folds": fold_reports, "grouped_OOF_PA": round(total / n, 6),
              "raw_train_PA": round(pa(rows, golds, 0.0), 6),
              "default_train_PA": round(pa(rows, golds, 0.45), 6),
              "median_train_threshold": recommended,
              "caution": "OOF evaluates threshold selection only; the agent/templates were not held out during development."}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
