#!/usr/bin/env python3
"""
Creation Review Pipeline CLI.

Automates the full review pipeline: clones/checks out the repo,
reads task files, runs Gemini-powered checks, and produces an HTML report.

Usage:
    # Single task
    python run_review.py --user-code 9h8no --task-code SOME_TASK --folder some_folder

    # Batch mode
    python run_review.py --batch tasks.csv

    # With options
    python run_review.py --user-code X --task-code Y --folder Z --output-dir ./reports --model gemini-3.1-pro-preview
"""

import argparse
import asyncio
import collections
import csv
import json
import os
import re
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import certifi

from prompts import (
    TEST_FILTER_CONSTRUCTS,
    FLAG_UNFAIR_TEST,
    TEST_FILTER_CONSTRUCTS_FILES,
    META_FILES_ALIGNMENT,
    META_FILES_ALIGNMENT_FILES,
    PROBLEM_STATEMENT_TEST_ALIGNMENT,
    PROBLEM_STATEMENT_TEST_ALIGNMENT_FILES,
    REQUIREMENTS_TEST_ALIGNMENT,
    REQUIREMENTS_TEST_ALIGNMENT_FILES,
    REQUIREMENTS_INTERFACE_ALIGNMENT,
    REQUIREMENTS_INTERFACE_ALIGNMENT_FILES,
    SCHEMA_VALIDATION,
    SCHEMA_VALIDATION_FILES,
    RUBRIC_VALIDATION,
    FUNCTIONAL_RUBRIC_VALIDATION_FILES,
    ROBUSTNESS_RUBRIC_VALIDATION_FILES,
    STYLE_RUBRIC_VALIDATION_FILES,
    RUBRIC_ALIGNMENT,
    FUNCTIONAL_RUBRIC_ALIGNMENT_FILES,
    ROBUSTNESS_RUBRIC_ALIGNMENT_FILES,
    META_FILE_IMPL_LEAK,
    META_FILE_IMPL_LEAK_FILES,
    SUFFICIENT_REQ_CHECK,
    SUFFICIENT_REQ_CHECK_FILES,
    PROMPTS,
)

# ==========================================
# CONFIG
# ==========================================

API_KEY = os.environ.get(
    "GEMINI_API_KEY"
)
DEFAULT_MODEL = "gemini-3.1-pro-preview"
MAX_API_CONCURRENCY = 50
MAX_RETRIES = 3
REQUEST_TIMEOUT = 120  # seconds per API call
GIT_TIMEOUT = 300  # seconds per git operation

# The six task files we read from each task folder
TASK_FILES = [
    ("rubric_json", "rubric/rubric.json"),
    ("prompt_statement_md", "prompt_statement.md"),
    ("problem_statement_md", "problem_statement.md"),
    ("interface_md", "interface.md"),
    ("requirements_json", "requirements.json"),
    ("test_patch", "test.patch"),
]

# SSL context for aiohttp (fixes macOS certificate issues)
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# Terminal colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

ERASE_LINE = "\033[K"
BAR_WIDTH = 30


class _ProgressBar:
    """In-place terminal progress bar for async tasks."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.in_flight = 0
        self._lock = asyncio.Lock()
        self._render("waiting...")

    def _render(self, label: str = ""):
        pct = int(100 * self.done / self.total) if self.total else 0
        filled = int(BAR_WIDTH * self.done / self.total) if self.total else 0
        bar = f"{'█' * filled}{'░' * (BAR_WIDTH - filled)}"
        flight = f" {YELLOW}{self.in_flight} running{RESET}" if self.in_flight else ""
        print(f"\r  [{bar}] {self.done}/{self.total} ({pct}%){flight} {label}{ERASE_LINE}", end="", flush=True)

    async def wrap(self, coro, name: str):
        async with self._lock:
            self.in_flight += 1
            self._render(f"{BOLD}+{name}{RESET}")
        try:
            result = await coro
        finally:
            async with self._lock:
                self.in_flight -= 1
                self.done += 1
                self._render(name)
        return result

    def finish(self):
        bar = "█" * BAR_WIDTH
        print(f"\r  [{bar}] {self.done}/{self.total} (100%) done{ERASE_LINE}")


# ==========================================
# 1. GIT OPERATIONS (replaces setup_task.sh)
# ==========================================


def _run_git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )


def clone_or_update_repo(user_code: str, task_code: str, base_dir: str = ".") -> Path:
    """Clone the repo if it doesn't exist, checkout the branch, and pull."""
    repo_name = f"agentic-bench-{user_code}"
    repo_path = Path(base_dir) / repo_name
    branch = f"agentic-bench-{task_code.lower()}"

    if not repo_path.exists():
        print(f"  Cloning {repo_name}...")
        _run_git(
            ["git", "clone", f"https://github.com/mercor-code-envs/{repo_name}.git"],
            cwd=base_dir,
        )

    print(f"  Checking out {branch}...")
    _run_git(["git", "checkout", branch], cwd=repo_path)
    _run_git(["git", "pull"], cwd=repo_path)

    return repo_path


def read_task_files(repo_path: Path, target_folder: str) -> tuple[dict, dict]:
    """Read the 6 task files. Returns (all_files dict, parsed rubric dict)."""
    full_path = repo_path / "swebench" / "tasks" / target_folder

    if not full_path.is_dir():
        raise FileNotFoundError(f"Task folder not found: {full_path}")

    raw = {}
    for key, rel_path in TASK_FILES:
        file_path = full_path / rel_path
        if file_path.exists():
            raw[key] = file_path.read_text(encoding="utf-8")
        else:
            raw[key] = ""
            print(f"  {YELLOW}Warning:{RESET} {rel_path} not found")

    # Build the all_files dict the checks expect
    try:
        rubric_dict = json.loads(raw["rubric_json"]) if raw["rubric_json"] else {}
    except json.JSONDecodeError as e:
        print(f"  {RED}Error:{RESET} Failed to parse rubric.json: {e}")
        rubric_dict = {}

    all_files = {
        **raw,
        "functional_rubric": rubric_dict.get("functional", []),
        "robustness_rubric": rubric_dict.get("robustness", []),
        "style_rubric": rubric_dict.get("style", []),
    }
    return all_files, rubric_dict


# ==========================================
# 2. SCHEMA VALIDATION (from notebook)
# ==========================================


def validate_schema(json_input: str) -> tuple[bool, list[str]]:
    """Validate rubric JSON structure. Identical logic to the notebook."""
    errors = []
    try:
        data = json.loads(json_input)
    except json.JSONDecodeError as e:
        return False, [f"Invalid JSON syntax: {e}"]

    REQUIRED_ROOT_KEYS = {"functional", "robustness", "style"}
    VALID_WEIGHTS = {"major", "minor"}
    VALID_SOURCES = {"prompt", "problem", "requirements"}
    COMMON_FIELDS = {"id", "description", "weight", "rationale", "dependent_on"}

    missing_root = REQUIRED_ROOT_KEYS - data.keys()
    if missing_root:
        errors.append(f"Missing required root keys: {sorted(missing_root)}")

    for cat in REQUIRED_ROOT_KEYS:
        items = data.get(cat)
        if items is None:
            continue
        if not isinstance(items, list):
            errors.append(f"Value for '{cat}' must be a list.")
            continue
        if len(items) < 2:
            errors.append(f"Category '{cat}' must contain at least 2 items.")

        for i, item in enumerate(items):
            pfx = f"[{cat}][{i+1}]"

            missing = COMMON_FIELDS - item.keys()
            if missing:
                errors.append(f"{pfx} missing fields: {sorted(missing)}")

            expected_id = f"{cat}-{i + 1}"
            if item.get("id") != expected_id:
                errors.append(
                    f"{pfx} Invalid ID '{item.get('id')}'. Expected '{expected_id}'."
                )

            dep = item.get("dependent_on")
            if dep is not None and not isinstance(dep, list):
                errors.append(f"{pfx} 'dependent_on' must be a list.")

            w = item.get("weight")
            if w is not None and w not in VALID_WEIGHTS:
                errors.append(
                    f"{pfx} Invalid weight '{w}'. Must be one of {VALID_WEIGHTS}"
                )

            src = item.get("source")
            if cat == "functional":
                if "source" not in item:
                    errors.append(f"{pfx} missing required field 'source'.")
                elif src not in VALID_SOURCES:
                    errors.append(
                        f"{pfx} Invalid source '{src}'. Must be one of {VALID_SOURCES}"
                    )
            elif "source" in item:
                errors.append(f"{pfx} Field 'source' is not applicable to '{cat}'.")

    return (True, ["JSON structure is valid."]) if not errors else (False, errors)


# ==========================================
# 3. API HELPER (from notebook)
# ==========================================


async def get_gemini_response(
    session: aiohttp.ClientSession,
    system_prompt: str,
    user_text: str,
    model: str = DEFAULT_MODEL,
    _semaphore: asyncio.Semaphore | None = None,
) -> str:
    """Call Gemini API with retry, backoff, and concurrency control."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "X-goog-api-key": API_KEY}
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
    }
    sem = _semaphore or asyncio.Semaphore(MAX_API_CONCURRENCY)
    last_error = ""

    for attempt in range(MAX_RETRIES):
        async with sem:
            try:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    body = await resp.text()
                    last_error = f"Error {resp.status}: {body}"
                    if resp.status not in _RETRYABLE_STATUSES:
                        return last_error
            except (KeyError, IndexError):
                return "Error: No content generated."
            except asyncio.TimeoutError:
                last_error = "Error: Request timed out."
            except aiohttp.ClientError as e:
                last_error = f"Request Failed: {e}"

        if attempt < MAX_RETRIES - 1:
            wait = 2 ** attempt + (attempt * 0.5)
            await asyncio.sleep(wait)

    return last_error


# ==========================================
# 4. CHECK RUNNERS (from notebook)
# ==========================================


def clean_json_response(text: str) -> str:
    clean = text.strip()
    return re.sub(r"^```(?:json)?|```$", "", clean, flags=re.MULTILINE).strip()


FAIRNESS_CHECKS = [
    ("FLAG_UNFAIR_TEST", FLAG_UNFAIR_TEST, TEST_FILTER_CONSTRUCTS_FILES),
    # ("TEST_REQ_FILTER", TEST_REQ_FILTER, TEST_FILTER_CONSTRUCTS_FILES),
    ("SCHEMA_VALIDATION", SCHEMA_VALIDATION, SCHEMA_VALIDATION_FILES),
    ("META_FILE_IMPL_LEAK", META_FILE_IMPL_LEAK, META_FILE_IMPL_LEAK_FILES),
    ("SUFFICIENT_REQ_CHECK", SUFFICIENT_REQ_CHECK, SUFFICIENT_REQ_CHECK_FILES),
    ("META_FILES_ALIGNMENT", META_FILES_ALIGNMENT, META_FILES_ALIGNMENT_FILES),
    ("PROBLEM_STATEMENT_TEST_ALIGNMENT", PROBLEM_STATEMENT_TEST_ALIGNMENT, PROBLEM_STATEMENT_TEST_ALIGNMENT_FILES),
    ("REQUIREMENTS_TEST_ALIGNMENT", REQUIREMENTS_TEST_ALIGNMENT, REQUIREMENTS_TEST_ALIGNMENT_FILES),
    ("REQUIREMENTS_INTERFACE_ALIGNMENT", REQUIREMENTS_INTERFACE_ALIGNMENT, REQUIREMENTS_INTERFACE_ALIGNMENT_FILES),
    ("FUNCTIONAL_RUBRIC_ALIGNMENT", RUBRIC_ALIGNMENT, FUNCTIONAL_RUBRIC_ALIGNMENT_FILES),
    ("ROBUSTNESS_RUBRIC_ALIGNMENT", RUBRIC_ALIGNMENT, ROBUSTNESS_RUBRIC_ALIGNMENT_FILES),
    ("FUNCTIONAL_RUBRIC_VALIDATION", RUBRIC_VALIDATION, FUNCTIONAL_RUBRIC_VALIDATION_FILES),
    ("ROBUSTNESS_RUBRIC_VALIDATION", RUBRIC_VALIDATION, ROBUSTNESS_RUBRIC_VALIDATION_FILES),
    ("STYLE_RUBRIC_VALIDATION", RUBRIC_VALIDATION, STYLE_RUBRIC_VALIDATION_FILES),
    ("TEST_FILTER_CONSTRUCTS", TEST_FILTER_CONSTRUCTS, TEST_FILTER_CONSTRUCTS_FILES),
]


async def run_fairness_checks(
    session: aiohttp.ClientSession,
    all_files: dict,
    model: str = DEFAULT_MODEL,
    _semaphore: asyncio.Semaphore | None = None,
    _progress: _ProgressBar | None = None,
) -> list[tuple[str, str]]:
    """Run all fairness checks in parallel with concurrency control."""
    # Format template values — rubric sub-categories need JSON serialization
    fmt_files = dict(all_files)
    for key in ("functional_rubric", "robustness_rubric", "style_rubric"):
        if isinstance(fmt_files[key], (list, dict)):
            fmt_files[key] = json.dumps(fmt_files[key], indent=2)

    names, coros = [], []
    for name, si, files_tpl in FAIRNESS_CHECKS:
        names.append(name)
        coro = get_gemini_response(session, si, files_tpl.format(**fmt_files), model, _semaphore)
        coros.append(_progress.wrap(coro, name) if _progress else coro)

    responses = await asyncio.gather(*coros)
    return list(zip(names, responses))


async def run_rubric_checks(
    session: aiohttp.ClientSession,
    rubric_data: dict,
    model: str = DEFAULT_MODEL,
    _semaphore: asyncio.Semaphore | None = None,
    _progress: _ProgressBar | None = None,
) -> list[tuple[str, str]]:
    """Run per-category rubric compliance checks."""
    categories = ["functional", "robustness", "style"]
    active, coros = [], []

    for cat in categories:
        items = rubric_data.get(cat, [])
        if not items:
            continue
        active.append(cat)
        payload = (
            "Here's the file you need to validate:\n\n"
            f"<rubrics>\n{json.dumps(items, indent=2)}\n</rubrics>"
        )
        coro = get_gemini_response(session, PROMPTS[cat], payload, model, _semaphore)
        coros.append(_progress.wrap(coro, f"{cat}_rubric") if _progress else coro)

    responses = await asyncio.gather(*coros)
    return list(zip(active, responses))


# ==========================================
# 5. REPORT RENDERING
# ==========================================


def md_to_html(text: str) -> str:
    """Convert Markdown-ish string to safe HTML (from notebook)."""
    if not text:
        return ""

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"`([^`\n]+)`", r'<code class="ic">\1</code>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    def _listify(m):
        items = re.findall(r"^[-*]\s+(.+)$", m.group(0), re.MULTILINE)
        li = "".join(f"<li>{it}</li>" for it in items)
        return f"<ul>{li}</ul>"

    text = re.sub(r"(?:^[-*]\s+.+$\n?)+", _listify, text, flags=re.MULTILINE)

    parts = re.split(r"(<ul>.*?</ul>)", text, flags=re.DOTALL)
    for idx, part in enumerate(parts):
        if not part.startswith("<ul>"):
            parts[idx] = part.replace("\n", "<br>")
    text = "".join(parts)
    return text


_REPORT_CSS = """
<style>
.rpt { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; }
.rpt h1 { font-size: 1.5em; margin-bottom: 4px; }
.rpt h2 { font-size: 1.2em; margin-top: 24px; padding-bottom: 4px; border-bottom: 2px solid #888; }
.rpt h3 { font-size: 1.05em; margin-top: 18px; }
.badge       { padding: 2px 10px; border-radius: 4px; font-weight: 700; font-size: .85em; display: inline-block; }
.badge-pass  { background: #d1fae5; color: #065f46; border: 1px solid #065f46; }
.badge-fail  { background: #fee2e2; color: #991b1b; border: 1px solid #991b1b; }
.badge-unk   { background: #e5e7eb; color: #374151; border: 1px solid #6b7280; }
.rpt table   { width: 100%; border-collapse: collapse; margin-top: 8px; }
.rpt th      { text-align: left; padding: 8px 10px; background: #374151; color: #f9fafb; border: 1px solid #4b5563; }
.rpt td      { padding: 8px 10px; border: 1px solid #d1d5db; vertical-align: top; }
.rpt tbody tr:nth-child(even) td { background: rgba(0,0,0,.03); }
.rpt summary { cursor: pointer; font-weight: 600; color: #2563eb; padding: 2px 0; }
.rpt details[open] summary { margin-bottom: 4px; }
.fb {
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: .88em;
    padding: 10px 12px;
    border-radius: 6px;
    margin-top: 4px;
    background: rgba(0,0,0,.06);
    border: 1px solid rgba(0,0,0,.12);
}
.fb .ic { background: rgba(0,0,0,.08); padding: 1px 5px; border-radius: 3px; font-size: .95em; }
.fb strong { font-weight: 700; }
.fb ul { margin: 4px 0 4px 18px; padding: 0; }
.fb li { margin-bottom: 2px; }
.summary-bar { display: flex; gap: 6px; align-items: center; margin: 6px 0 2px; }
.summary-bar .cnt { font-weight: 600; font-size: .95em; }
.parse-err { color: #dc2626; font-weight: 600; margin: 6px 0; }

/* ---------- Report header ---------- */
.rpt-meta { background: #f3f4f6; border: 1px solid #d1d5db; border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; font-size: .92em; }
.rpt-meta span { margin-right: 20px; }
.rpt-meta .label { color: #6b7280; }

/* ---------- Schema section ---------- */
.schema-pass { color: #065f46; font-weight: 600; }
.schema-fail { color: #991b1b; font-weight: 600; }
.schema-errors { margin: 4px 0 0 18px; padding: 0; }
.schema-errors li { font-size: .9em; color: #991b1b; margin-bottom: 2px; }
</style>
"""


def _badge(status: str) -> str:
    s = status.strip().upper()
    if "PASS" in s:
        return '<span class="badge badge-pass">PASS</span>'
    if "FAIL" in s:
        return '<span class="badge badge-fail">FAIL</span>'
    return f'<span class="badge badge-unk">{s}</span>'


def render_html_report(
    fairness_results: list[tuple[str, str]],
    rubric_results: list[tuple[str, str]],
    schema_valid: bool = True,
    schema_messages: list[str] | None = None,
    user_code: str = "",
    folder: str = "",
) -> str:
    """Render a standalone HTML report (identical CSS/rendering as notebook)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    h = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Validation Report</title>"
        f"{_REPORT_CSS}</head><body>\n"
        '<div class="rpt">\n<h1>Unified Validation Report</h1>\n'
    )

    # Report metadata header
    h += '<div class="rpt-meta">'
    if user_code:
        h += f'<span><span class="label">User:</span> <strong>{user_code}</strong></span>'
    if folder:
        h += f'<span><span class="label">Folder:</span> <strong>{folder}</strong></span>'
    h += f'<span><span class="label">Generated:</span> {timestamp}</span>'
    h += "</div>\n"

    # Section 0: Schema Validation
    h += "<h2>0 - Schema Validation</h2>\n"
    if schema_valid:
        h += '<p class="schema-pass">VALID</p>\n'
    else:
        h += '<p class="schema-fail">INVALID</p>\n'
    if schema_messages:
        h += '<ul class="schema-errors">'
        for msg in schema_messages:
            h += f"<li>{md_to_html(msg)}</li>"
        h += "</ul>\n"

    # Section 1: Fairness Checks
    h += "<h2>1 - High-Level Fairness Checks</h2>\n"
    h += (
        "<table><thead><tr>"
        "<th style='width:22%'>Check</th>"
        "<th>Details</th>"
        "</tr></thead><tbody>\n"
    )
    for name, raw in fairness_results:
        body = md_to_html(raw)
        h += (
            f"<tr><td><strong>{name}</strong></td>"
            f"<td><details><summary>View Feedback</summary>"
            f'<div class="fb">{body}</div></details></td></tr>\n'
        )
    h += "</tbody></table>\n"

    # Section 2: Rubric Checks
    h += "<h2>2 - Rubric Compliance</h2>\n"
    for category, raw_json in rubric_results:
        h += f"<h3>{category.capitalize()}</h3>\n"
        try:
            items = json.loads(clean_json_response(raw_json))
            if not isinstance(items, list):
                raise ValueError("Expected a JSON array")

            n_pass = sum(
                1 for x in items if str(x.get("status", "")).upper() == "PASS"
            )
            n_total = len(items)
            h += (
                '<div class="summary-bar">'
                f'<span class="cnt">{n_pass}/{n_total} Passed</span>'
                "</div>\n"
            )
            h += (
                "<table><thead><tr>"
                "<th style='width:10%'>ID</th>"
                "<th style='width:10%'>Status</th>"
                "<th>Feedback</th>"
                "</tr></thead><tbody>\n"
            )
            for item in items:
                fid = item.get("id", "\u2014")
                st = item.get("status", "UNKNOWN")
                feedback = item.get("feedback", "")
                fix = item.get("proposed_fix", "")
                parts = []
                if feedback:
                    parts.append(feedback)
                if fix:
                    parts.append(f"**Proposed fix:** {fix}")
                combined = "\n".join(parts) if parts else "\u2014"
                body = md_to_html(combined)
                h += (
                    f"<tr><td>{fid}</td>"
                    f"<td>{_badge(st)}</td>"
                    f'<td><div class="fb">{body}</div></td></tr>\n'
                )
            h += "</tbody></table>\n"
        except Exception as e:
            h += f'<p class="parse-err">Failed to parse response for {category}: {e}</p>\n'
            h += (
                f"<details><summary>Raw output</summary>"
                f'<div class="fb">{md_to_html(raw_json)}</div></details>\n'
            )

    h += "</div>\n</body></html>"
    return h


def save_report(html: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")


def _status_label(raw: str) -> str:
    """Determine pass/fail from raw response text."""
    upper = raw.strip().upper()
    for keyword in ("PASS", "ALIGNED", "FAIR", "NO_VIOLATIONS"):
        if keyword in upper.split("\n")[0]:
            return "PASS"
    for keyword in ("FAIL", "MISALIGNED", "UNFAIR"):
        if keyword in upper.split("\n")[0]:
            return "FAIL"
    return "UNKNOWN"


def print_terminal_summary(
    fairness_results: list[tuple[str, str]],
    rubric_results: list[tuple[str, str]],
) -> None:
    """Print colored pass/fail summary to stdout."""
    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  VALIDATION SUMMARY{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")

    print(f"{BOLD}Fairness Checks:{RESET}")
    for name, raw in fairness_results:
        status = _status_label(raw)
        if status == "PASS":
            icon = f"{GREEN}PASS{RESET}"
        elif status == "FAIL":
            icon = f"{RED}FAIL{RESET}"
        else:
            icon = f"{YELLOW}????{RESET}"
        print(f"  {icon}  {name}")

    print(f"\n{BOLD}Rubric Compliance:{RESET}")
    for category, raw_json in rubric_results:
        try:
            items = json.loads(clean_json_response(raw_json))
            n_pass = sum(
                1 for x in items if str(x.get("status", "")).upper() == "PASS"
            )
            n_total = len(items)
            color = GREEN if n_pass == n_total else RED
            print(f"  {color}{n_pass}/{n_total}{RESET}  {category.capitalize()}")
        except Exception:
            print(f"  {YELLOW}????{RESET}  {category.capitalize()} (parse error)")

    print()


# ==========================================
# 6. SCHEMA CHECK (terminal output)
# ==========================================


def run_schema_check(all_files: dict) -> tuple[bool, list[str]]:
    """Run the local schema validation and print results."""
    is_valid, messages = validate_schema(all_files["rubric_json"])
    status = f"{GREEN}VALID{RESET}" if is_valid else f"{RED}INVALID{RESET}"
    print(f"\n  Schema: {status}")
    for m in messages:
        print(f"    - {m}")
    return is_valid, messages


# ==========================================
# 7. ORCHESTRATOR
# ==========================================


async def process_task(
    user_code: str,
    task_code: str,
    folder: str,
    output_dir: str = "./reports",
    model: str = DEFAULT_MODEL,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Full pipeline for one task."""
    print(f"\n{BOLD}Processing: {user_code} / {folder}{RESET}")

    # Step 1: Git operations (run in thread to avoid blocking the event loop)
    repo_path = await asyncio.to_thread(clone_or_update_repo, user_code, task_code)

    # Step 2: Read files (returns parsed rubric_data to avoid re-parsing)
    all_files, rubric_data = read_task_files(repo_path, folder)

    # Step 3: Schema check
    schema_valid, schema_messages = run_schema_check(all_files)

    # Step 4: Run all checks
    n_fairness = len(FAIRNESS_CHECKS)
    n_rubric = sum(1 for cat in ("functional", "robustness", "style") if rubric_data.get(cat))
    total_checks = n_fairness + n_rubric
    print(f"\n  Running {n_fairness} fairness + {n_rubric} rubric = {total_checks} checks")
    t0 = time.monotonic()

    owns_session = session is None
    if owns_session:
        timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT, connect=15, sock_connect=10, sock_read=REQUEST_TIMEOUT,
        )
        connector = aiohttp.TCPConnector(
            ssl=SSL_CTX, limit=0, limit_per_host=MAX_API_CONCURRENCY,
            ttl_dns_cache=300, keepalive_timeout=30,
        )
        session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    sem = asyncio.Semaphore(MAX_API_CONCURRENCY)
    progress = _ProgressBar(total_checks)
    try:
        fairness_res, rubric_res = await asyncio.gather(
            run_fairness_checks(session, all_files, model, sem, progress),
            run_rubric_checks(session, rubric_data, model, sem, progress),
        )
    finally:
        if owns_session:
            await session.close()

    progress.finish()
    elapsed = time.monotonic() - t0
    print(f"  Completed in {elapsed:.1f}s")

    # Step 5: Terminal summary
    print_terminal_summary(fairness_res, rubric_res)

    # Step 6: HTML report
    html = render_html_report(
        fairness_res, rubric_res,
        schema_valid=schema_valid,
        schema_messages=schema_messages,
        user_code=user_code,
        folder=folder,
    )
    report_path = Path(output_dir) / f"{user_code}_{folder}_report.html"
    save_report(html, report_path)
    print(f"  Report saved: {report_path}")


async def process_batch(
    csv_path: str,
    output_dir: str = "./reports",
    model: str = DEFAULT_MODEL,
) -> None:
    """Read CSV and process all tasks concurrently."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    n_tasks = len(rows)
    print(f"Batch mode: {n_tasks} task(s) from {csv_path}\n")

    # ── Phase 1: Prepare tasks (git + file read) ──
    # Per-repo locks prevent concurrent branch checkouts on the same clone.
    repo_locks = collections.defaultdict(asyncio.Lock)

    async def _prepare(row):
        user_code, task_code, folder = row["user_code"], row["task_code"], row["folder"]
        async with repo_locks[user_code]:
            repo_path = await asyncio.to_thread(clone_or_update_repo, user_code, task_code)
        all_files, rubric_data = read_task_files(repo_path, folder)
        schema_valid, schema_messages = run_schema_check(all_files)
        return {
            "row": row,
            "all_files": all_files,
            "rubric_data": rubric_data,
            "schema_valid": schema_valid,
            "schema_messages": schema_messages,
        }

    print(f"  Preparing {n_tasks} task(s) (git clone/checkout + file read)...")
    prepared = await asyncio.gather(*[_prepare(row) for row in rows])
    print(f"  All tasks prepared.\n")

    # ── Phase 2: Run all API checks concurrently ──
    total_checks = 0
    for p in prepared:
        total_checks += len(FAIRNESS_CHECKS)
        total_checks += sum(1 for cat in ("functional", "robustness", "style") if p["rubric_data"].get(cat))

    print(f"  Running {total_checks} total checks across {n_tasks} task(s)")
    t0 = time.monotonic()

    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT, connect=15, sock_connect=10, sock_read=REQUEST_TIMEOUT,
    )
    connector = aiohttp.TCPConnector(
        ssl=SSL_CTX, limit=0, limit_per_host=MAX_API_CONCURRENCY,
        ttl_dns_cache=300, keepalive_timeout=30,
    )
    sem = asyncio.Semaphore(MAX_API_CONCURRENCY)
    progress = _ProgressBar(total_checks)

    async def _run_checks(p):
        fairness_res, rubric_res = await asyncio.gather(
            run_fairness_checks(session, p["all_files"], model, sem, progress),
            run_rubric_checks(session, p["rubric_data"], model, sem, progress),
        )
        return {**p, "fairness_res": fairness_res, "rubric_res": rubric_res}

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        results = await asyncio.gather(*[_run_checks(p) for p in prepared])

    progress.finish()
    elapsed = time.monotonic() - t0
    print(f"  All checks completed in {elapsed:.1f}s\n")

    # ── Phase 3: Generate reports ──
    for i, r in enumerate(results, 1):
        row = r["row"]
        user_code, folder = row["user_code"], row["folder"]

        print(f"{BOLD}[{i}/{n_tasks}] {user_code} / {folder}{RESET}")
        print_terminal_summary(r["fairness_res"], r["rubric_res"])

        html = render_html_report(
            r["fairness_res"], r["rubric_res"],
            schema_valid=r["schema_valid"],
            schema_messages=r["schema_messages"],
            user_code=user_code,
            folder=folder,
        )
        report_path = Path(output_dir) / f"{user_code}_{folder}_report.html"
        save_report(html, report_path)
        print(f"  Report saved: {report_path}\n")


# ==========================================
# 8. CLI ENTRY POINT
# ==========================================


def main():
    parser = argparse.ArgumentParser(
        description="Creation Review Pipeline — run Gemini-powered validation checks on task artifacts."
    )

    # Single-task args
    parser.add_argument("--user-code", help="User code (e.g., 9h8no)")
    parser.add_argument("--task-code", help="Task/branch code (e.g., SOME_TASK)")
    parser.add_argument("--folder", help="Target folder under swebench/tasks/")

    # Batch mode
    parser.add_argument(
        "--batch", help="Path to CSV file with columns: user_code,task_code,folder"
    )

    # Options
    parser.add_argument(
        "--output-dir", default="./reports", help="Output directory for HTML reports (default: ./reports)"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"Gemini model to use (default: {DEFAULT_MODEL})"
    )

    args = parser.parse_args()

    # Validate: either single-task args or --batch, not both
    single_task_args = [args.user_code, args.task_code, args.folder]
    has_single = any(single_task_args)
    has_batch = args.batch is not None

    if has_single and has_batch:
        parser.error("Cannot use both single-task args and --batch.")
    if not has_single and not has_batch:
        parser.error("Provide either --user-code/--task-code/--folder or --batch.")
    if has_single and not all(single_task_args):
        parser.error("--user-code, --task-code, and --folder are all required together.")

    if not API_KEY:
        print(f"{RED}Error:{RESET} GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    if has_batch:
        asyncio.run(process_batch(args.batch, args.output_dir, args.model))
    else:
        asyncio.run(
            process_task(args.user_code, args.task_code, args.folder, args.output_dir, args.model)
        )


if __name__ == "__main__":
    main()
