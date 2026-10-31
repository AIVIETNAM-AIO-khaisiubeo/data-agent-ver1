# Getting Started

This guide covers installation, tests, inference, evaluation, calibration, and the Docker submission path. For the architecture and design rationale, see the [repository design overview](../DESIGN.md) and [detailed design](docs/DESIGN.md).

## 1. Requirements

- Python 3.10 or newer.
- A compatible GPU setup and vLLM for the full Qwen3-14B inference path.
- The official split files. See [Data Layout](docs/DATA_LAYOUT.md) for the expected structure.

The template-and-rules path runs without a GPU or an LLM. A complete Qwen3-14B GPU run is not represented in the currently recorded results.

## 2. Install and test

Run these commands from the `datamate-agent/` directory:

```bash
python -m pip install -r requirements.txt
make test
```

The test suite includes unit tests and an end-to-end run on a synthetic split. It does not require a GPU.

## 3. Run on a labeled training split

To generate candidate answers and traces for calibration:

```bash
python scripts/run.py \
  --data path/to/train \
  --out runs/train_submission.jsonl \
  --dump runs/train_dump.jsonl
```

The split must contain its question file, active rulebook, optional calendar, and business data in the structure described in [Data Layout](docs/DATA_LAYOUT.md).

## 4. Full LLM inference path

The default configuration uses `Qwen/Qwen3-14B` (14.8B parameters) behind an OpenAI-compatible vLLM endpoint. The model-size check enforces a configured total parameter budget of 15B when the LLM backend is enabled.

Start the server on a machine with two suitable GPUs:

```bash
vllm serve Qwen/Qwen3-14B \
  --tensor-parallel-size 2 \
  --enable-prefix-caching \
  --max-model-len 32768 \
  --seed 0
```

Use the same model and inference configuration for both calibration traces and the submission run.

### 4.1 Fit the calibrator

First, run the agent on labeled training data with `--dump` as shown in Section 3. Then fit the calibrator:

```bash
python scripts/fit_calibrator.py \
  runs/train_dump.jsonl \
  path/to/train/answers.jsonl \
  --out artifacts/calibrator.pkl
```

The script uses grouped cross-validation by business for its out-of-fold diagnostics and fits the final artifact on labeled candidate examples. Do not use the offline template prior as a substitute for calibrating an LLM-enabled run.

### 4.2 Generate a public-test submission

```bash
python scripts/run.py \
  --data path/to/public_test \
  --out submission.jsonl \
  --logs runs/public_logs
```

The runner writes results incrementally and preserves input order in the final output. Per-question logs include the final answer, elapsed time, intent and time context, candidate programs, and execution outputs.

## 5. Template-and-rules mode (no LLM)

Use this mode for low-compute evaluation or as a fallback. It does not require GPU inference:

```bash
python scripts/run.py \
  --data path/to/public_test \
  --out submission.jsonl \
  --logs runs/public_logs \
  --set llm.backend=none \
  --set decision.calibrator_path=artifacts/calibrator_offline.pkl
```

This mode has lower coverage for questions outside the supported deterministic templates. Refer to [Evaluation Results](docs/RESULTS.md) for its measured performance and limitations.


## 5.1 Round 1 local run (CPU / no LLM)

On a machine without a supported inference GPU, run the deterministic templates-and-rules path:

```powershell
.\scripts\run_round1_local.ps1
```

The script defaults to `../official_data/public_test`, disables the LLM, writes `submission.jsonl`,
records a diagnostic dump under `artifacts/`, and validates exact question-ID coverage. You can
override the data folder, output path, worker count, and local wall-clock budget:

```powershell
.\scripts\run_round1_local.ps1 -DataPath "..\official_data\public_test" -Workers 2 -TimeBudgetSeconds 86400
```

The local CPU profile is a development submission, not a substitute for the final competition
Docker inference profile. Do not use public-test labels (which are not provided) for fitting or
threshold selection. Calibration must use labeled training data only.

To inspect or fit calibration using the labeled output of `eval_templates.py`:

```powershell
.\.venv\Scripts\python.exe .\scripts\fit_calibrator.py `
    .\artifacts\train_eval_commit6.jsonl `
    --out .\artifacts\calibrator_round1_offline.pkl `
    --offline-prior 0.75
```

When all candidate labels have the same value, the script saves a prior-only calibrator and reports
that a discriminative calibration cannot be learned; it does not invent incorrect examples.

## 6. Evaluate a labeled split

To evaluate deterministic templates and calculate PA:

```bash
python scripts/eval_templates.py \
  --split path/to/train \
  --out runs/train_offline_dump.jsonl

python scripts/evaluate.py \
  submission_train.jsonl \
  path/to/train/answers.jsonl
```

PA assigns +1 for a correct answer, −1 for a wrong answer, and 0 for abstention. Abstentions remain in the denominator. Keep in-sample training results separate from grouped-fold calibration estimates and from unlabeled public-test output coverage.

## 7. Docker submission path

`predict.sh` reads data from `/data`, writes `/result/submission.jsonl`, and saves per-question logs to `/result/logs/<id>.json`. The script starts vLLM with tensor parallelism across two GPUs, enables offline mode through `HF_HUB_OFFLINE=1`, and switches to the template path after the configured fast-path cutoff to respect the run budget.

`Dockerfile` is a starting template. Adapt the base image to the competition's Docker instructions and ensure that the CUDA runtime matches the target GPU; the original target setup required CUDA 12.8 or newer for NVIDIA Blackwell.

## 8. Configuration

Common configuration lives in `configs/default.yaml`; split-specific business conventions can be overridden in `configs/rulebook_overrides.yaml`. Override values from the command line with repeated `--set` flags, for example:

```bash
python scripts/run.py \
  --data path/to/public_test \
  --out runs/public.jsonl \
  --set llm.backend=none \
  --set agent.use_templates=false
```

Inspect `scripts/run.py` and `datamate/agent.py` before changing inference behavior. The runner derives per-question seeds from question text, the as-of date, and candidate family rather than question IDs.

## 9. Documentation reading order

- [System Design Overview](../DESIGN.md) — short architecture and operating constraints.
- [Detailed Design](docs/DESIGN.md) — module responsibilities, research rationale, calibration, and limitations.
- [Data Layout](docs/DATA_LAYOUT.md) — expected split and business-file structure.
- [Evaluation Results](docs/RESULTS.md) — measured results, error analysis, and reproduction notes.
- [Research References](docs/REFERENCES.md) — bibliography for the design citations.

## 10. PA-focused validation (2026-10-31)

The PA metric rewards correct answers (+1), penalizes incorrect ones (-1), and
gives zero for `abstain`. A rule of thumb is to answer only when the estimated
probability of being correct is above 0.5 **after calibration on held-out
training businesses**. The default is now `decision.min_template_strength=0.0` (**disabled**) because
an actual 3,000-question TRAIN run at 0.45 dropped from PA 0.888 to 0.825:
correct 2,475, wrong 0, abstain 525. The previous train run had
correct 2,667, wrong 3, abstain 330. These are **in-sample** figures;
no new public-test PA has been measured.

This branch adds three inference safeguards:

1. SQL samples from one prompt do not count as independent confirmation.
   When no deterministic result supports the answer, a second agent family
   (plan-to-SQL, Python, or independent SQL model) must agree to early-exit.
2. Scalar answer types must come from one result row. Numeric outputs must
   also have an unambiguous numeric column; otherwise the execution-feedback
   repair path receives a format failure.
3. A **configurable, disabled-by-default** low-strength template gate is
   available for calibrated deployments. An unsupported LLM-only
   `unanswerable` answer requires independent confirmation or positive evidence.

### Train-only grouped PA threshold study

From `datamate-agent/`, create an *unfiltered candidate dump* on the
official labeled **train** split. The dump contains raw `pred` candidates,
features and business IDs, not test answers:

```bash
mkdir -p runs
python scripts/run.py --data /path/to/train --out runs/train.jsonl \
  --dump runs/train_dump.jsonl --set llm.backend=none

python scripts/sweep_policy.py runs/train_dump.jsonl \
  /path/to/train/answers.jsonl --folds 5 \
  --calibrator artifacts/calibrator_offline.pkl \
  --submitted runs/train.jsonl \
  --out runs/train_business_grouped_pa.json
```

For a quick CPU-only candidate dump, `eval_templates.py --out` is also
supported. `sweep_policy.py` tunes the template-strength gate **inside
training folds** and measures it on *other* businesses, then reports the
median selected threshold. Re-test after changing the agent: grouped threshold
selection alone does not remove earlier template-development leakage.

The grouped PA report **replays the complete PA decision** from the saved
candidate features using the original calibrator; it does not require another
inference run. It compares 0.00 vs 0.45 thresholds and a fold-selected threshold. It never inspects
public/private reference answers. Set `--set decision.min_template_strength=X`
after inspecting train-held-out behavior. Keep the official public-test
leaderboard for occasional verification only, not as a training target.

**Run the regression suite:** `python -m pytest -q tests`. A complete
LLM-enabled validation additionally needs an offline Qwen3-14B checkpoint,
a compatible GPU/vLLM stack, and freshly fitted calibration traces from
that same inference configuration. A local template-only run cannot establish
the accuracy of the LLM-enabled agent.

### Diagnose remaining TRAIN abstentions by intent and type

Use the previously saved predictions, candidate dump, and per-question logs:

```bash
python scripts/diagnose_train.py \
  --split /path/to/train --submission runs/train.jsonl \
  --dump runs/train_dump.jsonl --logs runs/train_logs \
  --show 25 --out runs/train_misses.json
```

The JSON report groups missing answers by reference type and classified
intent, separating absent candidates from candidates rejected by a decision
policy. Inspect these summaries *before* adding templates for stock, debt,
expiry, boolean, or table questions. Do not use PUBLIC/PRIVATE test labels
for feature selection, hard-coded answers, or threshold fitting.
