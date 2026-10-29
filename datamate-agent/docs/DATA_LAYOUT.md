# Data Layout

This document defines the input structure expected by DataMate Agent. Use it when preparing a train, development, or test split, or when debugging a shop whose files are not being discovered correctly.

For setup and run commands, see [Getting Started](../GETTING_STARTED.md).

## 1. Split directory

Each split should contain a question file, the rulebook for that split, an optional solar–lunar calendar, and a data folder containing one directory per business.

```text
<split>/
├── questions.jsonl
├── rulebook.md
├── calendar.csv
└── data/
    ├── <business_id_1>/
    │   ├── README.md
    │   ├── sales.csv
    │   ├── products.xlsx
    │   └── database.sqlite
    └── <business_id_2>/
        ├── README.md
        └── ...
```

The example file names are illustrative. The loader supports CSV, Excel workbooks, and SQLite databases; it does not require all three formats for every business.

## 2. Question records

`questions.jsonl` contains one JSON object per line with the following fields:

| Field | Purpose |
|---|---|
| `id` | Unique identifier for the question in the split. |
| `business_id` | Identifier used to locate the corresponding business data directory. |
| `question` | The question to answer, typically phrased in Vietnamese. |
| `as_of_date` | Reference date used to resolve relative time expressions. |

Example:

```json
{"id":"q_001","business_id":"biz_001","question":"What was the revenue last week?","as_of_date":"2026-03-04"}
```

The IDs identify records and businesses; the implementation should not use them as answer lookups.

## 3. Rulebook and calendar

### Rulebook

Place the split's authoritative rulebook in a shared file named `rulebook.md`, `rulebook.txt`, `rulebook.yaml`, or `rulebook.json`. The runtime can also discover rulebook files by name under the split root. Rulebooks may differ between splits; do not assume the train rulebook is also authoritative for public or private test data.

The rulebook describes business conventions such as valid-order semantics, revenue calculations, rounding, time periods, ranking metrics, and output requirements. The active rulebook is passed to the LLM and informs which deterministic templates are allowed to run.

### Solar–lunar calendar

Place the official calendar file in a shared CSV file whose name includes `lunar`, `am_lich`, or `calendar`, or supply an explicit path through configuration. When provided, this file takes precedence over the fallback Vietnamese lunar-calendar algorithm.

## 4. Business directory

Use the `business_id` value from each question record as the folder name under `data/`. A business directory can contain:

- **CSV files** for tabular exports.
- **Excel files** (`.xlsx`) with one or more sheets, including monthly sheets or multiple tables.
- **SQLite database files** (`.sqlite` or equivalent supported extensions).
- **`README.md`** documenting table meanings, column descriptions, unit conversions, date coverage, and other business-specific conventions.

The business README is input metadata used during schema/column-role inference. It is different from the repository-level documentation files.

The loader attempts to handle common real-world irregularities: title rows, multi-row headers, repeated headers, total rows, mixed date/currency formats, quantity values with units, and data spread across sheets or files. It is still worth inspecting loader warnings and canonical-view consistency checks for unfamiliar schemas.

## 5. Synthetic split

The repository's `make synth` command generates a synthetic split with the same overall layout and a `gold.jsonl` file for testing. Synthetic data is suitable for unit and end-to-end smoke tests; it is not a substitute for evaluation against the official labeled split or for validation on unseen businesses.

## 6. Troubleshooting checklist

- Confirm that `questions.jsonl` exists at the split root and each line is valid JSON.
- Check that every `business_id` in the questions maps to a discoverable business directory.
- Confirm the rulebook filename and content correspond to the split being evaluated.
- Check that the calendar file, if present, is named so the loader can discover it or is explicitly configured.
- Keep a shop's supporting README next to that shop's data files.
- Inspect the per-question log and loader/view warnings if a table, column, date, amount, or entity does not seem to be interpreted correctly.
