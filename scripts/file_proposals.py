#!/usr/bin/env python3
"""Files top candidate code issues as proposal issues on the forked repository.

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

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scanners.ast_auditor import CodebaseAuditor
from scanners.test_auditor import TestAuditor


def run_command(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def get_existing_proposal_titles(fork_slug: str) -> set[str]:
    """Fetches titles of open/closed proposal issues to prevent duplicates."""
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


def file_proposals(repo_path: str, fork_slug: str, max_proposals: int = 5) -> None:
    print(f"Scanning {repo_path} to file up to {max_proposals} proposal issues on {fork_slug}...")
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

        title = f"[Proposal] {f['rule']}: {f['summary']} ({f['file']}:{f['line']})"
        if title in existing_titles:
            continue

        body = (
            f"## Automated Fix Proposal by `gcsfs-expert`\n\n"
            f"**Rule**: `{f['rule']}` ({f['severity']})\n"
            f"**Location**: `{f['file']}:{f['line']}`\n\n"
            f"### Problem Description\n{f['summary']}\n\n"
            f"### Proposed Fix\n{f['suggestion']}\n\n"
            f"---\n"
            f"👉 **To approve this fix**: Reply to this issue with **`/approve`** or **`approve`**.\n"
            f"The dedicated VM will automatically create a branch, write the code, verify with `pytest`, "
            f"and submit a Pull Request to upstream `fsspec/gcsfs`."
        )

        res = run_command([
            "gh", "issue", "create",
            "--repo", fork_slug,
            "--title", title,
            "--body", body,
            "--label", "proposal"
        ], check=False)

        if res.returncode == 0:
            print(f"Created proposal issue: {res.stdout.strip()}")
            existing_titles.add(title)
            created_count += 1
        else:
            print(f"Failed creating issue: {res.stderr}")

    print(f"Done. Filed {created_count} proposal issue(s) on {fork_slug}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="File candidate issue proposals on forked repository.")
    parser.add_argument("--repo", default="/home/princer_google_com/c2dev/gcsfs", help="Path to local target repo")
    parser.add_argument("--fork-slug", default="raj-prince/gcsfs", help="Target forked GitHub repo (owner/repo)")
    parser.add_argument("--max-proposals", type=int, default=5, help="Maximum proposal issues to file")
    args = parser.parse_args()

    file_proposals(repo_path=args.repo, fork_slug=args.fork_slug, max_proposals=args.max_proposals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
