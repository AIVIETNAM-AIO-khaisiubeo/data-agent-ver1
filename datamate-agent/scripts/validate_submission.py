"""Validate submission JSONL structure and exact question-ID coverage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datamate.formatter import ANSWER_TYPES  # noqa: E402


def read_jsonl(path: Path):
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_no}: blank line is not allowed")
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{line_no}: each JSONL row must be an object")
            rows.append(item)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--submission", required=True)
    args = ap.parse_args()

    questions = read_jsonl(Path(args.questions))
    submission = read_jsonl(Path(args.submission))
    qids = [row.get("id") for row in questions]
    sids = [row.get("id") for row in submission]
    if any(value is None for value in qids):
        raise SystemExit("Questions file contains a row without id")
    if any(value is None for value in sids):
        raise SystemExit("Submission contains a row without id")
    if len(set(qids)) != len(qids):
        raise SystemExit("Questions file contains duplicate IDs")
    if len(set(sids)) != len(sids):
        raise SystemExit("Submission contains duplicate IDs")

    qset, sset = set(qids), set(sids)
    missing, extra = qset - sset, sset - qset
    errors = []
    allowed = set(ANSWER_TYPES) | {"abstain", "unanswerable"}
    for line_no, row in enumerate(submission, 1):
        kind = row.get("type")
        if kind not in allowed:
            errors.append(f"line {line_no}: unsupported type {kind!r}")
        elif kind not in {"abstain", "unanswerable"} and "answer" not in row:
            errors.append(f"line {line_no}: type {kind!r} is missing answer")

    if missing:
        errors.append(f"missing {len(missing)} question IDs; examples: {list(sorted(missing, key=str))[:5]}")
    if extra:
        errors.append(f"{len(extra)} unknown question IDs; examples: {list(sorted(extra, key=str))[:5]}")
    if len(questions) != len(submission):
        errors.append(f"row count mismatch: questions={len(questions)}, submission={len(submission)}")
    if errors:
        print("SUBMISSION INVALID")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print("SUBMISSION VALID")
    print(f"Rows: {len(submission)}")
    print(f"Unique IDs: {len(sset)}")
    counts = {}
    for row in submission:
        counts[row["type"]] = counts.get(row["type"], 0) + 1
    for kind, count in sorted(counts.items()):
        print(f"{kind}: {count}")


if __name__ == "__main__":
    main()
