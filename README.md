# Creation Task Validator

CLI tool that automates the creation review pipeline — clones task repos, reads artifacts, runs Gemini-powered validation checks, and produces an HTML report.

## Setup

```bash
pip install -r requirements.txt
```

Set your API key (optional — falls back to the hardcoded default):
```bash
export GEMINI_API_KEY="your-key-here"
```

## Usage

### Single task
```bash
python run_review.py --user-code 9h8no --task-code SOME_TASK --folder some_folder
```

### Batch mode
```bash
python run_review.py --batch tasks.csv
```

### Options
```bash
python run_review.py --user-code X --task-code Y --folder Z \
  --output-dir ./reports \
  --model gemini-3.1-pro-preview
```

| Flag | Description | Default |
|------|-------------|---------|
| `--user-code` | User code (e.g., `9h8no`) | — |
| `--task-code` | Task/branch code | — |
| `--folder` | Target folder under `swebench/tasks/` | — |
| `--batch` | Path to CSV (`user_code,task_code,folder`) | — |
| `--output-dir` | Output directory for HTML reports | `./reports` |
| `--model` | Gemini model | `gemini-3.1-pro-preview` |

## What it does

1. **Git ops** — Clones/checks out the `agentic-bench-{user_code}` repo and the task branch
2. **Reads files** — `rubric.json`, `prompt_statement.md`, `problem_statement.md`, `interface.md`, `requirements.json`, `test.patch`
3. **Schema validation** — Validates rubric JSON structure locally
4. **Fairness checks** (14 parallel Gemini calls) — Test fairness, file alignment, implementation leak detection, requirements sufficiency, rubric validation/alignment
5. **Rubric compliance** (3 Gemini calls) — Per-category (functional, robustness, style) rubric validation
6. **Report** — Colored terminal summary + standalone HTML report in `reports/`

## Files

| File | Purpose |
|------|---------|
| `run_review.py` | CLI entry point + all logic |
| `prompts.py` | Gemini prompt templates |
| `tasks.csv` | Example CSV for batch mode |
