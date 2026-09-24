#!/usr/bin/env python3
"""Autonomous Sentry Engine for gcsfs.

Fully automated loop:
1. Clones or updates the target repository (fsspec/gcsfs).
2. Audits the codebase using AST scanners.
3. Checks existing GitHub PRs to prevent duplicate submissions.
4. Selects the highest priority issue.
5. Employs the gcsfs-expert agent to generate a minimal, safe fix.
6. Verifies the fix against the local GCS emulator using pytest.
7. Submits a Pull Request to GitHub if all verification passes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scanners.ast_auditor import CodebaseAuditor
from scanners.test_auditor import TestAuditor


def run_command(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    print(f">> {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def check_existing_prs(repo_slug: str, rule: str) -> bool:
    """Checks if a PR for this rule is already open."""
    try:
        res = run_command(["gh", "pr", "list", "--repo", repo_slug, "--state", "open", "--json", "title"], check=False)
        if res.returncode == 0:
            prs = json.loads(res.stdout)
            for pr in prs:
                if rule in pr.get("title", ""):
                    return True
    except Exception:
        pass
    return False


def run_sentry(target_repo_path: str, repo_slug: str = "fsspec/gcsfs", dry_run: bool = False) -> int:
    repo_path = Path(target_repo_path).resolve()
    print(f"=== Starting Autonomous Sentry on {repo_path} ({repo_slug}) ===")

    # 1. Update main branch
    print("1. Syncing with main branch...")
    run_command(["git", "checkout", "main"], cwd=str(repo_path))
    run_command(["git", "pull", "--ff-only"], cwd=str(repo_path), check=False)

    # 2. Run AST and Test Scanners
    print("2. Scanning for issues...")
    source_auditor = CodebaseAuditor(root_dir=str(repo_path))
    test_auditor = TestAuditor(tests_dir=str(repo_path / "gcsfs" / "tests"))

    findings = source_auditor.audit() + test_auditor.audit()
    print(f"Found {len(findings)} potential improvements.")

    # Filter for candidate issue (HIGH severity first)
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_findings = sorted(findings, key=lambda x: severity_order.get(x.get("severity", "LOW"), 99))

    candidate = None
    for f in sorted_findings:
        # Check if already PR'd
        if not check_existing_prs(repo_slug, f["rule"]):
            candidate = f
            break

    if not candidate:
        print("No eligible candidate issues to fix at this time.")
        return 0

    print(f"\nTarget Selected for Automated Fix:")
    print(f"  ID:       {candidate['id']}")
    print(f"  Rule:     {candidate['rule']} ({candidate['severity']})")
    print(f"  Location: {candidate['file']}:{candidate['line']}")
    print(f"  Issue:    {candidate['summary']}\n")

    # 3. Create Feature Branch
    branch_name = f"bot/{candidate['rule']}-L{candidate['line']}"
    print(f"3. Creating branch '{branch_name}'...")
    run_command(["git", "checkout", "-B", branch_name], cwd=str(repo_path))

    # 4. Generate Fix with Agent
    print("4. Invoking gcsfs-expert agent...")
    skill_file = BASE_DIR / "skills" / "gcsfs-expert" / "SKILL.md"
    instructions = ""
    if skill_file.is_file():
        instructions = f"Follow the rules in {skill_file}. "

    prompt = (
        f"{instructions}Fix issue {candidate['id']} in {candidate['file']} at line {candidate['line']}. "
        f"Issue description: {candidate['summary']}. Suggestion: {candidate['suggestion']}. "
        "Keep the fix minimal and backward-compatible with fsspec."
    )

    agent_cmd = ["agy", "-p", prompt]
    agent_res = run_command(agent_cmd, cwd=str(repo_path), check=False)
    print(agent_res.stdout[:500] if agent_res.stdout else "")

    # 5. Check if git diff exists
    diff_res = run_command(["git", "status", "--porcelain"], cwd=str(repo_path))
    if not diff_res.stdout.strip():
        print("Agent did not produce changes. Aborting.")
        run_command(["git", "checkout", "main"], cwd=str(repo_path))
        return 1

    # 6. Verify with Pytest
    print("5. Running pytest verification...")
    # Map file to corresponding test
    test_target = "gcsfs/tests/test_core.py"
    test_res = run_command(["pytest", test_target, "-q", "--maxfail=1"], cwd=str(repo_path), check=False)
    if test_res.returncode != 0:
        print(f"Tests failed during verification:\n{test_res.stdout[:500]}\nAborting PR.")
        run_command(["git", "checkout", "."], cwd=str(repo_path))
        run_command(["git", "checkout", "main"], cwd=str(repo_path))
        return 1

    print("Pytest passed successfully!")

    # 7. Commit & Create PR
    if dry_run:
        print("[DRY-RUN] Changes verified. Skipping git push and gh pr create.")
        return 0

    print("6. Committing and opening Pull Request...")
    commit_msg = f"fix({Path(candidate['file']).stem}): {candidate['summary']}"
    run_command(["git", "commit", "-am", commit_msg], cwd=str(repo_path))
    run_command(["git", "push", "origin", branch_name, "--force"], cwd=str(repo_path))

    pr_body = (
        f"## Automated Fix by `gcsfs-expert` Sentry\n\n"
        f"**Rule**: `{candidate['rule']}` ({candidate['severity']})\n"
        f"**File**: `{candidate['file']}:{candidate['line']}`\n\n"
        f"### Summary\n{candidate['summary']}\n\n"
        f"### Rationale\n{candidate['suggestion']}\n\n"
        f"### Verification\n- Ran pytest against local GCS emulator: **PASSED**."
    )

    pr_res = run_command([
        "gh", "pr", "create",
        "--repo", repo_slug,
        "--title", commit_msg,
        "--body", pr_body,
        "--head", branch_name,
        "--base", "main"
    ], cwd=str(repo_path), check=False)

    if pr_res.returncode == 0:
        print(f"Successfully raised PR: {pr_res.stdout.strip()}")
    else:
        print(f"gh pr create failed: {pr_res.stderr}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Sentry for gcsfs.")
    parser.add_argument("--repo", default="/home/princer_google_com/c2dev/gcsfs", help="Path to gcsfs repository")
    parser.add_argument("--target-slug", default="fsspec/gcsfs", help="Target GitHub repo slug (owner/repo)")
    parser.add_argument("--dry-run", action="store_true", help="Audit and verify fix without pushing PR")
    args = parser.parse_args()

    return run_sentry(target_repo_path=args.repo, repo_slug=args.target_slug, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
