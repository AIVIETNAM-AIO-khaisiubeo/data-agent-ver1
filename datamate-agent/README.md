# DataMate Agent — Zalo AI Challenge 2026

DataMate Agent answers everyday Vietnamese business questions over small-shop data stored in CSV, Excel, and SQLite files. It is designed to **answer when the evidence is sufficient and abstain when it is not**.

The challenge uses Penalized Accuracy (PA): a correct answer scores +1, a wrong answer scores −1, and abstention scores 0.

Example input and output:

```json
{"id": "q_06de33fa21cc", "business_id": "biz_118", "question": "sp bán dc nhiều nhất tuần rồi là gì", "as_of_date": "2026-03-04"}
→ {"id": "q_06de33fa21cc", "type": "string", "answer": "Xi măng Hà Tiên PCB40 bao 50kg"}
```

- [Detailed system design](docs/DESIGN.md)
- [Evaluation results and limitations](docs/RESULTS.md)
- [Research references](docs/REFERENCES.md)

## Key design ideas

1. **PA-aware abstention.** With scores of +1/0/−1, the theoretical decision is to answer when the estimated probability of correctness is greater than 0.5. The calibrator is evaluated with cross-validation grouped by shop so questions from the same shop are not split across fitting and validation folds.
2. **Canonical business views.** The loader handles many file layouts and builds `v_sales`, `v_orders`, `v_products`, and `v_purchases` where the source data supports them. Order validity, returns, revenue-line treatment, base-unit conversion, and walk-in-customer handling are applied centrally. View consistency checks become reliability features.
3. **Rulebook-aware templates.** The active split's rulebook is passed to the LLM. A deterministic template runs only if the active rulebook contains the clauses it implements; otherwise, it is disabled rather than applying outdated rules.
4. **Deterministic handling where practical.** Relative periods, Tet windows, morning/afternoon shifts, tie-breaking, rounding, and answer formatting are handled explicitly instead of being left entirely to an LLM.
5. **Diverse candidate families.** The agent combines templates with optional SQL, plan-to-SQL, Python/pandas, and ReAct candidates. It executes candidates and compares their normalized answers, and can use execution feedback to repair failures.
6. **Operational guardrails.** The configured inference path supports offline operation, checks the declared 15B model-parameter budget, derives per-question seeds from question context, writes output incrementally, and records candidate traces in per-question logs.

## Recorded results

The figures below describe the **template-and-rules, LLM-free mode**. They are not results for a full Qwen3-14B GPU run.

| Split | Correct | Wrong | Abstain | PA |
|---|---:|---:|---:|---:|
| Train (3,000 questions; in-sample) | 2,667 | 3 | 330 | 0.888 |
| Public (1,000 questions; no labels available) | 611 typed answers + 24 `unanswerable` | — | 365 | Not available |

The train result is in-sample: training failures were inspected during development and used to improve general-purpose parsing and data handling. Public outputs have no ground truth here, so output coverage and formatting checks must not be described as public-test accuracy or PA.

The first-round submission snapshot is available at [`submissions/public_test_round1.jsonl`](submissions/public_test_round1.jsonl). See [Results](docs/RESULTS.md) for the complete table, interpretation, known fixes, runtime observations, and reproduction commands.

## Installation and tests

Use Python 3.10 or newer. From this directory:

```bash
python -m pip install -r requirements.txt
make test
```

The test suite includes unit tests and an end-to-end run on a synthetic split and does not require a GPU.

## Public test: generate `submission.jsonl`

A split follows this layout:

```text
<split>/
├── questions.jsonl
├── rulebook.md
├── calendar.csv
└── data/
    └── <business_id>/
        ├── *.csv
        ├── *.xlsx
        ├── *.sqlite
        └── README.md
```

### A. Full LLM path (GPU; recommended for the intended inference setup)

Start an OpenAI-compatible vLLM server using two GPUs and tensor parallelism:

```bash
vllm serve Qwen/Qwen3-14B \
  --tensor-parallel-size 2 \
  --enable-prefix-caching \
  --max-model-len 32768 \
  --seed 0
```

Run the agent on labeled train data, fit the calibrator on those traces, then run public test with the same inference configuration:

```bash
python scripts/run.py \
  --data path/to/train \
  --out runs/train_sub.jsonl \
  --dump runs/train_dump.jsonl

python scripts/fit_calibrator.py \
  runs/train_dump.jsonl \
  path/to/train/answers.jsonl \
  --out artifacts/calibrator.pkl

python scripts/run.py \
  --data path/to/public_test \
  --out submission.jsonl \
  --logs runs/public_logs
```

**Do not skip calibrator fitting for an LLM submission.** The default/offline prior is not a substitute for a calibrator trained on traces from the actual LLM mode.

### B. Template-and-rules path (no GPU/LLM)

```bash
python scripts/run.py \
  --data path/to/public_test \
  --out submission.jsonl \
  --logs runs/public_logs \
  --set llm.backend=none \
  --set decision.calibrator_path=artifacts/calibrator_offline.pkl
```

### Evaluate a labeled split

```bash
python scripts/eval_templates.py \
  --split path/to/train \
  --out runs/train_offline_dump.jsonl

python scripts/evaluate.py \
  submission_train.jsonl \
  path/to/train/answers.jsonl
```

The evaluator reports PA under the local reimplementation of the official scoring rules. Keep in-sample evaluation separate from grouped-fold estimates and from any public/hidden test claims.

## Docker submission path

`predict.sh` reads input from `/data`, writes `/result/submission.jsonl`, and saves per-question logs under `/result/logs/<id>.json`. The script starts vLLM with tensor parallelism across two GPUs, sets `HF_HUB_OFFLINE=1`, and switches to the template path after the configured cutoff to stay within the time budget.

`Dockerfile` is a starting template. Adapt its base image to the competition's Docker instructions; NVIDIA Blackwell GPUs require a compatible CUDA runtime (the README's original target was CUDA 12.8 or newer).

## Declared inference model

| Model | Parameters | License | Release | Intended role |
|---|---:|---|---|---|
| `Qwen/Qwen3-14B` | 14.8B | Apache-2.0 | April 2025 | SQL/Python generation, ReAct exploration, and candidate judging |

The configured total is 14.8B parameters, below the 15B limit. No embedding model, reranker, or additional classifier is declared by default. The run script checks declared model sizes when the LLM backend is enabled.

## Code map

| Path | Responsibility |
|---|---|
| `datamate/textnorm.py` | Normalizes Vietnamese strings; includes metric normalization, Telex handling, and Vietnamese money/quantity parsing. |
| `datamate/loader.py` | Reads CSV/XLSX/SQLite into DuckDB; handles headers, monthly sheets, mixed dates/currencies/quantities, and caching. |
| `datamate/readme.py` | Parses the shop README for the column dictionary, business conventions, data range, and unit-conversion notes. |
| `datamate/roles.py` | Assigns column roles from README evidence, headers, and value types; classifies tables. |
| `datamate/salesview.py` | Builds `v_sales`, `v_orders`, `v_products`, and `v_purchases`; applies rulebook-aware logic, consistency checks, and name-to-code aliases. |
| `datamate/attributes.py` | Converts catalog attributes such as active ingredients, brands, and prescription flags into filters. |
| `datamate/rulebook.py` | Loads split-specific rule text, infers structured conventions, and fingerprints rule clauses required by templates. |
| `datamate/lunar.py` | Resolves Vietnamese lunar dates using the supplied calendar or fallback algorithm. |
| `datamate/timeresolver.py` | Converts natural-language time expressions into explicit intervals. |
| `datamate/question.py` | Extracts intent, answer type, top-k, thresholds, and filters. |
| `datamate/linker.py` | Links question mentions to data values and detects missing or ambiguous entities. |
| `datamate/answerability.py` | Collects evidence that the data cannot answer a question. |
| `datamate/templates.py` | Implements deterministic programs for supported business-question patterns. |
| `datamate/prompts.py` | Defines contracts and prompts for SQL, planning, Python, ReAct, repair, and judging. |
| `datamate/executor.py` | Executes read-only DuckDB SQL with timeouts and runs Python candidates in a separate process. |
| `datamate/formatter.py` | Converts execution results to typed answers and canonical comparison keys. |
| `datamate/agent.py` | Orchestrates the end-to-end per-question pipeline. |
| `datamate/calibrate.py` | Maps confidence features to estimated correctness and supports threshold analysis. |
| `datamate/scorer.py` | Implements the local PA scoring rules used for evaluation. |
| `datamate/synthetic.py` | Generates synthetic splits for testing. |
| `scripts/` | Entry points for running, evaluating, calibrating, serving, and inspecting the pipeline. |

## Known limitations

- A full Qwen3-14B run on the target GPU setup is not represented in the recorded results. The LLM path has been smoke-tested with a simulated backend and a 1.5B model on CPU, which does not establish final accuracy or throughput.
- The calibrator must be fitted again when the inference mode or candidate distribution changes.
- In template-only mode, question types outside the supported sales and purchase patterns—such as debt, stock, lots/expiry, delivery, and specialized industries—have limited coverage and may be abstained.
- The reported train PA is in-sample. New shops and new business types may be harder.
- The public split has no ground truth in this repository, so public coverage is not an accuracy metric.
- The loader's handling of public data was improved after inspecting general file-format failures, but this does not establish that any unobserved schema will be parsed perfectly.
