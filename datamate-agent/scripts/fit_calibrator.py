"""Fit/evaluate a calibrator with business-grouped out-of-fold predictions.

Accepts either:
  A) run.py dump + a separate answers.jsonl file; or
  B) eval_templates.py --out JSONL, which already contains ``correct`` labels.

A one-class dataset cannot train a discriminative calibrator. In that case this
script reports the limitation and saves a prior-only artifact instead of
crashing or fabricating negative examples.
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamate.calibrate import Calibrator, best_threshold, expected_pa  # noqa: E402
from datamate.scorer import is_correct  # noqa: E402


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="run.py dump or eval_templates.py --out JSONL")
    ap.add_argument("gold", nargs="?", help="answers.jsonl; omit when dump already has correct labels")
    ap.add_argument("--out", default="artifacts/calibrator.pkl")
    ap.add_argument("--C", type=float, default=0.5)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--offline-prior", type=float, default=0.75)
    ap.add_argument("--min-isotonic", type=int, default=300)
    a = ap.parse_args()

    rows = read_jsonl(a.dump)
    golds = {d["id"]: d for d in read_jsonl(a.gold)} if a.gold else None
    n_total = 0
    feats, labels, groups = [], [], []

    for row in rows:
        if golds is not None:
            if row.get("id") not in golds:
                continue
            n_total += 1
            pred = row.get("pred") or {}
            if pred.get("type") in (None, "abstain") or not row.get("feats"):
                continue
            label = int(is_correct(pred, golds[row["id"]]))
        else:
            if "correct" not in row:
                raise SystemExit("dump has no 'correct' labels; provide the gold answers.jsonl as the second argument")
            n_total += 1
            pred = row.get("pred") or {}
            if pred.get("type") in (None, "abstain") or not row.get("feats"):
                continue
            label = int(bool(row["correct"]))

        feats.append(row["feats"])
        labels.append(label)
        groups.append(str(row.get("business_id", "?")))

    y = np.asarray(labels, dtype=int)
    cal = Calibrator(a.offline_prior)
    print(f"{len(y)} candidate questions out of {n_total}; candidate accuracy {y.mean() if len(y) else 0:.3f}")
    print(f"business groups: {len(set(groups))}")

    if len(y) == 0 or len(np.unique(y)) < 2:
        cal.save(a.out)
        reason = "no candidate examples" if len(y) == 0 else "only one candidate-label class"
        print(f"WARNING: {reason}; cannot learn P(correct). Saved prior-only calibrator to {a.out}.")
        print("No wrong examples were fabricated; use business-held-out validation and retain the configured threshold.")
        return

    unique_groups = sorted(set(groups))
    n_splits = min(max(2, a.folds), len(unique_groups))
    if n_splits < 2:
        cal.fit(feats, y.tolist(), C=a.C)
        cal.save(a.out)
        print("Only one business group; saved logistic model without OOF isotonic calibration.")
        return

    from sklearn.model_selection import GroupKFold
    oof = np.zeros(len(y), dtype=float)
    oof_filled = np.zeros(len(y), dtype=bool)
    splitter = GroupKFold(n_splits=n_splits)
    for train_idx, test_idx in splitter.split(np.zeros(len(y)), y, groups):
        fold_y = y[train_idx]
        if len(np.unique(fold_y)) < 2:
            fold_cal = Calibrator(a.offline_prior)
        else:
            fold_cal = Calibrator(a.offline_prior).fit([feats[i] for i in train_idx], fold_y.tolist(), C=a.C)
        oof[test_idx] = [fold_cal.p(feats[i]) for i in test_idx]
        oof_filled[test_idx] = True

    prior = np.asarray([Calibrator(a.offline_prior).prior(f) for f in feats])
    scale = len(y) / max(1, n_total)
    print(f"answer-every-candidate PA         = {(2 * y.mean() - 1) * scale:.4f}")
    print(f"prior @0.5 (all questions)        = {expected_pa(prior, y, 0.5) * scale:.4f}")
    print(f"logistic OOF @0.5 (all questions) = {expected_pa(oof, y, 0.5) * scale:.4f}")
    tau, pa = best_threshold(oof, y)
    print(f"OOF exploratory best tau={tau:.2f}; PA={pa * scale:.4f} (threshold chosen on these same OOF scores; optimistic)")

    per = defaultdict(lambda: [0, 0, 0])
    for feat, label, prob in zip(feats, y, oof):
        key = feat.get("intent") or "-"
        per[key][0] += 1
        per[key][1] += int(label)
        per[key][2] += 1 if prob > 0.5 and label else (-1 if prob > 0.5 else 0)
    print(f"\n{'intent':28s}{'n':>6s}{'acc':>8s}{'PA@0.5':>9s}")
    for key, (n, ok, score) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        print(f"{key:28s}{n:6d}{ok / n:8.3f}{score / n:9.3f}")

    cal.fit(feats, y.tolist(), C=a.C)
    if (len(y) >= a.min_isotonic and len(np.unique(y)) == 2 and oof_filled.all()
            and len(np.unique(np.round(oof, 8))) > 1):
        from sklearn.isotonic import IsotonicRegression
        cal.iso = IsotonicRegression(out_of_bounds="clip").fit(oof, y)
        print(f"Isotonic calibration fitted on {len(y)} grouped OOF probabilities.")
    else:
        print("Isotonic calibration skipped (insufficient OOF sample size or class diversity).")
    cal.save(a.out)
    print(f"Saved calibrator: {a.out}")


if __name__ == "__main__":
    main()
