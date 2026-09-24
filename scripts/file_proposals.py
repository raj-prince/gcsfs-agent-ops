#!/usr/bin/env python3
"""Files detailed, actionable candidate issue proposals on the forked repository.

Usage:
    python scripts/file_proposals.py --repo /path/to/gcsfs --fork-slug raj-prince/gcsfs --max-proposals 5
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scanners.ast_auditor import CodebaseAuditor
from scanners.test_auditor import TestAuditor


def run_command(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def get_existing_proposal_titles(fork_slug: str) -> set[str]:
    titles = set()
    res = run_command(
        ["gh", "issue", "list", "--repo", fork_slug, "--state", "all", "--json", "title", "--limit", "100"],
        check=False
    )
    if res.returncode == 0:
        data = json.loads(res.stdout)
        for item in data:
            titles.add(item.get("title", ""))
    return titles


def get_code_snippet(repo_path: str, rel_file: str, line_no: int, window: int = 5) -> str:
    """Extracts code snippet with line numbers."""
    full_path = os.path.join(repo_path, rel_file)
    if not os.path.isfile(full_path):
        return ""
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        start = max(1, line_no - window)
        end = min(len(lines), line_no + window)
        snippet = []
        for i in range(start, end + 1):
            marker = "--> " if i == line_no else "    "
            snippet.append(f"{marker}{i:4d}: {lines[i-1].rstrip()}")
        return "\n".join(snippet)
    except Exception:
        return ""


def get_rule_details(rule: str) -> dict[str, str]:
    """Provides rich domain context and real-world risk analysis for known rules."""
    details = {
        "async-create-task": {
            "title": "Unreferenced Fire-and-Forget Task",
            "risk": (
                "- **Premature Garbage Collection**: In Python asyncio, `create_task()` creates only a weak reference internally. "
                "If the task object is not stored, Python's GC can garbage-collect and cancel the task before the coroutine finishes.\n"
                "- **Swallowed Exceptions**: Errors raised in unreferenced tasks are not caught by the caller and only log a generic "
                "`Task exception was never retrieved` upon process cleanup."
            ),
            "test_target": "gcsfs/tests/test_close_from_event_loop.py"
        },
        "error-no-silent-exceptions": {
            "title": "Silent Exception Swallowing",
            "risk": (
                "- **Hidden Failures**: Silently swallowing exceptions (`except ...: pass`) masks real I/O and network errors, "
                "making debugging extremely difficult in production and potentially causing silent data loss.\n"
                "- **Contract Ambiguity**: Callers receive None or incomplete state instead of explicit error boundaries."
            ),
            "test_target": "gcsfs/tests/test_core.py"
        },
        "error-exception-chaining": {
            "title": "Missing Exception Cause Chaining",
            "risk": (
                "- **Broken Tracebacks**: Raising a new exception inside an `except` block without `from e` destroys the root causal chain.\n"
                "- **Observability Loss**: Sentry, Cloud Logging, and bug monitors cannot group or diagnose the underlying system error."
            ),
            "test_target": "gcsfs/tests/test_core.py"
        },
        "mock-autospec": {
            "title": "Mock Lacks autospec=True",
            "risk": (
                "- **False Positive Tests**: Mocks created without `autospec=True` accept ANY method call and argument signature. "
                "If production method signatures change, the tests continue to pass falsely, allowing regressions into releases."
            ),
            "test_target": "gcsfs/tests/test_core.py"
        }
    }
    return details.get(rule, {
        "title": rule,
        "risk": "Violates gcsfs coding standards and maintainability best practices.",
        "test_target": "gcsfs/tests/test_core.py"
    })


def file_proposals(repo_path: str, fork_slug: str, max_proposals: int = 3) -> None:
    print(f"Auditing {repo_path} to file up to {max_proposals} detailed proposals on {fork_slug}...")
    source_auditor = CodebaseAuditor(root_dir=repo_path)
    test_auditor = TestAuditor(tests_dir=str(Path(repo_path) / "gcsfs" / "tests"))

    findings = source_auditor.audit() + test_auditor.audit()

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_findings = sorted(findings, key=lambda x: severity_order.get(x.get("severity", "LOW"), 99))

    existing_titles = get_existing_proposal_titles(fork_slug)
    created_count = 0

    for f in sorted_findings:
        if created_count >= max_proposals:
            break

        rule_info = get_rule_details(f["rule"])
        title = f"[Proposal] {rule_info['title']} in {f['file']}:{f['line']}"
        if title in existing_titles:
            continue

        snippet = get_code_snippet(repo_path, f["file"], f["line"])

        body = (
            f"# {rule_info['title']}\n\n"
            f"> [!IMPORTANT]\n"
            f"> **Severity**: `{f['severity']}` | **Location**: `{f['file']}:{f['line']}` | **Rule**: `{f['rule']}`\n\n"
            f"---\n\n"
            f"### 1. Executive Summary\n"
            f"{f['summary']}\n\n"
            f"### 2. Real-World Production Risk\n"
            f"{rule_info['risk']}\n\n"
            f"### 3. Code Location\n"
            f"```python\n{snippet}\n```\n\n"
            f"### 4. Proposed Solution\n"
            f"{f['suggestion']}\n\n"
            f"### 5. Verification Plan\n"
            f"- Test suite target: `pytest {rule_info['test_target']}`\n"
            f"- Validated against local GCS emulator before PR submission.\n\n"
            f"---\n\n"
            f"### 🚦 Triage Decision\n"
            f"* 👉 **To Approve**: Reply to this issue with **`/approve`** or **`approve`**.\n"
            f"* ❌ **To Reject**: Close this issue or reply **`/reject`**."
        )

        res = run_command([
            "gh", "issue", "create",
            "--repo", fork_slug,
            "--title", title,
            "--body", body,
            "--label", "proposal"
        ], check=False)

        if res.returncode == 0:
            print(f"Created detailed proposal: {res.stdout.strip()}")
            existing_titles.add(title)
            created_count += 1
        else:
            print(f"Failed creating issue: {res.stderr}")

    print(f"Complete. Created {created_count} detailed proposal(s).")


def main() -> int:
    parser = argparse.ArgumentParser(description="File detailed proposals on forked repo.")
    parser.add_argument("--repo", default="/home/princer_google_com/c2dev/gcsfs", help="Path to local target repo")
    parser.add_argument("--fork-slug", default="raj-prince/gcsfs", help="Fork repo (owner/repo)")
    parser.add_argument("--max-proposals", type=int, default=3, help="Max proposals to file")
    args = parser.parse_args()

    file_proposals(repo_path=args.repo, fork_slug=args.fork_slug, max_proposals=args.max_proposals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
