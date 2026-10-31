Commit 7 patch
==============

Files to copy into the existing datamate-agent project, preserving folder paths:
- datamate/calibrate.py (replace)
- scripts/fit_calibrator.py (replace)
- scripts/run_round1_local.ps1 (new)
- scripts/validate_submission.py (new)
- tests/test_calibrate_commit7.py (new)
- GETTING_STARTED.md (replace)

Behavioral change:
- When LLM is disabled and the winning candidate agrees with a fired template,
  confidence is the configured offline_template_prior. Template strength remains
  a selection/ranking feature but is no longer multiplied into the probability.
- This specifically addresses the previous public-test dump where 288 candidates
  had been generated but rejected because prior * tpl_strength was <= 0.45.
- This is supported by the user's earlier training evaluation: 2,698 template
  candidates correct and 0 wrong. It remains a training-set observation, not a
  guarantee of public/private-test accuracy. Check public leaderboard performance.

Calibration changes:
- LogisticRegression.fit no longer fits IsotonicRegression on in-sample outputs.
- fit_calibrator.py accepts eval_templates.py --out data directly, or the older
  run.py dump plus answers.jsonl format.
- Business-grouped OOF probabilities are used for diagnostics and, when there
  are enough examples and non-constant OOF probabilities, for isotonic fitting.
- If candidate labels have only one class, a prior-only artifact is saved rather
  than exiting or fabricating labels.
- Calibrator.load now restores offline_prior from the artifact.

Local Round 1:
- run_round1_local.ps1 runs public_test with llm.backend=none and CPU workers=2.
- It writes submission.jsonl, dump/log artifacts, then validates exact ID coverage,
  duplicate IDs, JSONL syntax, answer types, and required answer fields.

Do not commit public/private test data, artifacts, logs, or submission.jsonl.
