"""Replay PA policies on TRAIN candidate traces without running inference again.

Usage (from datamate-agent/):
  python scripts/sweep_policy.py runs/train_dump.jsonl /path/train/answers.jsonl \
      --calibrator artifacts/calibrator_offline.pkl --submitted runs/train_submission.jsonl \
      --out runs/pa_policy_analysis.json

Uses ONLY labeled TRAIN answers. Groups are business-disjoint when selecting
thresholds, but the underlying handwritten templates were developed on train,
so the resulting grouped scores are not an unseen-business benchmark.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import numpy as np
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamate.calibrate import Calibrator  # noqa: E402
from datamate.scorer import report, score_one  # noqa: E402

TYPES = {"number", "percent", "string", "date", "boolean", "list", "table"}
GRID = [round(i * 0.05, 2) for i in range(20)]


def read_jsonl(path):
    with open(path, encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def replay(row, threshold, calibrator, tau_answer=0.5, tau_ua=0.5):
    """Reconstruct Agent.decide for a candidate under an alternative strength gate.

    Use the *raw* prediction and original features. Do not use row['p'],
    because the previous decision routine capped p at 0.49 after abstaining.
    """
    pred, f = row.get("pred") or {}, row.get("feats") or {}
    typ = pred.get("type")
    if not f or typ not in TYPES | {"unanswerable"}:
        return {"type": "abstain"}
    p = calibrator.p(f)
    if typ == "unanswerable":
        if p > tau_ua:
            out = {"type": typ}
            if pred.get("reason"):
                out["reason"] = pred["reason"]
            return out
        return {"type": "abstain"}
    if p <= tau_answer:
        return {"type": "abstain"}
    if (not f.get("llm_used") and f.get("tpl_agree")
            and float(f.get("tpl_strength", 0)) < threshold):
        return {"type": "abstain"}
    if "answer" not in pred:
        return {"type": "abstain"}
    return {"type": typ, "answer": pred["answer"]}


def predictions(rows, threshold, cal, tau_answer, tau_ua):
    return {row["id"]: replay(row, threshold, cal, tau_answer, tau_ua) for row in rows}


def calc_pa(rows, golds, threshold, cal, tau_answer, tau_ua):
    return sum(score_one(replay(row, threshold, cal, tau_answer, tau_ua), golds[row["id"]])
               for row in rows) / max(1, len(rows))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="Raw train candidate dump from run.py --dump or eval_templates.py --out")
    ap.add_argument("gold", help="TRAIN answers.jsonl ONLY")
    ap.add_argument("--calibrator", default="artifacts/calibrator_offline.pkl",
                    help="Same calibrator used by the run, default offline artifact")
    ap.add_argument("--submitted", help="TRAIN submission.jsonl to verify replay against original results")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--tau-answer", type=float, default=0.5)
    ap.add_argument("--tau-ua", type=float, default=0.5)
    ap.add_argument("--out", help="JSON report filename")
    ap.add_argument("--show", type=int, default=12, help="Max newly-recovered train examples to display")
    a = ap.parse_args()

    golds = {x["id"]: x for x in read_jsonl(a.gold)}
    rows = [x for x in read_jsonl(a.dump) if x.get("id") in golds]
    if not rows:
        raise SystemExit("No labeled TRAIN questions matched. Check the dump and answers paths.")
    if len({row["id"] for row in rows}) != len(rows):
        raise SystemExit("Duplicate question IDs in candidate dump")
    cal = Calibrator.load(a.calibrator)
    pa = lambda rs, t: calc_pa(rs, golds, t, cal, a.tau_answer, a.tau_ua)

    zero = predictions(rows, 0.0, cal, a.tau_answer, a.tau_ua)
    guarded = predictions(rows, 0.45, cal, a.tau_answer, a.tau_ua)
    r0 = report(zero, {r["id"]: golds[r["id"]] for r in rows})
    r45 = report(guarded, {r["id"]: golds[r["id"]] for r in rows})

    gains = []
    type_gains = Counter()
    for row in rows:
        qid = row["id"]
        if guarded[qid]["type"] == "abstain" and zero[qid]["type"] != "abstain":
            score = score_one(zero[qid], golds[qid])
            type_gains[(golds[qid]["type"], score)] += 1
            gains.append({"business_id": row.get("business_id"), "reference_type": golds[qid]["type"],
                          "prediction_type": zero[qid]["type"], "score": score,
                          "template_strength": row.get("feats", {}).get("tpl_strength")})

    submitted = None
    if a.submitted:
        preds = {x["id"]: x for x in read_jsonl(a.submitted)}
        submitted = report(preds, {r["id"]: golds[r["id"]] for r in rows})
        disagreement = sum(preds.get(r["id"]) != guarded[r["id"]] for r in rows)
    else:
        disagreement = None

    groups = [str(x.get("business_id") or "") for x in rows]
    folds = []
    oof_total, oof_n = 0.0, 0
    if all(groups) and len(set(groups)) >= 2:
        splitter = GroupKFold(n_splits=min(max(2, a.folds), len(set(groups))))
        for a_idx, b_idx in splitter.split(np.zeros(len(rows)), groups=groups):
            train_rows = [rows[i] for i in a_idx]
            heldout = [rows[i] for i in b_idx]
            # Tie: choose the less restrictive threshold to retain coverage.
            chosen = max(GRID, key=lambda t: (pa(train_rows, t), -t))
            held_pa = pa(heldout, chosen)
            folds.append({"train_n": len(train_rows), "heldout_n": len(heldout),
                          "heldout_business_count": len({groups[i] for i in b_idx}),
                          "best_training_threshold": chosen,
                          "heldout_PA_at_selected_threshold": round(held_pa, 6),
                          "heldout_PA_at_zero": round(pa(heldout, 0.0), 6),
                          "heldout_PA_at_0_45": round(pa(heldout, 0.45), 6)})
            oof_total += held_pa * len(heldout)
            oof_n += len(heldout)

    result = {
        "note": "TRAIN ONLY. Threshold evaluation is not a held-out evaluation of previously developed templates.",
        "candidate_count": len(rows),
        "original_submission": submitted,
        "original_vs_045_replay_mismatches": disagreement,
        "threshold_0": r0,
        "threshold_0_45": r45,
        "recovered_by_relaxing_0_45_to_0": {
            "total": len(gains), "correct": sum(x["score"] == 1 for x in gains),
            "wrong": sum(x["score"] == -1 for x in gains),
            "by_reference_type_and_score": {f"{k[0]}:{k[1]}": v for k, v in type_gains.items()},
            "examples": gains[:max(0, a.show)],
        },
        "folds": folds,
        "business_grouped_threshold_OOF_PA": round(oof_total / oof_n, 6) if oof_n else None,
        "grid": [{"min_template_strength": t, "train_PA": round(pa(rows, t), 6)} for t in GRID]
    }
    print("TRAIN PA replay (no inference rerun)")
    print(f"  threshold 0.00 : PA={r0['PA']:.5f} correct={r0['correct']} wrong={r0['wrong']} abstain={r0['abstain']}")
    print(f"  threshold 0.45 : PA={r45['PA']:.5f} correct={r45['correct']} wrong={r45['wrong']} abstain={r45['abstain']}")
    print(f"  newly released : {len(gains)} ({sum(x['score'] == 1 for x in gains)} correct, "
          f"{sum(x['score'] == -1 for x in gains)} wrong)")
    if submitted:
        print(f"  observed file  : PA={submitted['PA']:.5f}; replay mismatches={disagreement}")
        if disagreement:
            print("  WARNING: original file differs from 0.45 replay. Check calibrator/config and run version.")
    if folds:
        print(f"  grouped threshold-selection OOF PA={result['business_grouped_threshold_OOF_PA']:.5f}"
              " (NOT independently held-out agent generalization)")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"  report written to {a.out}")


if __name__ == "__main__":
    main()
