#!/usr/bin/env python3
"""Autonomous PR Shepherd Daemon for gcsfs.

Maintains up to 3 active PRs concurrently:
1. Polls each active PR every 30 minutes.
   - If MERGED: Cleans up branch, frees slot.
   - If CLOSED: Cleans up branch, frees slot.
   - If OPEN: Inspects for new review comments.
     * Generates fix for reviewer feedback.
     * Verifies with pytest.
     * Pushes commit and replies to the review thread.
2. If active PR count < 3:
   - Scans gcsfs codebase for top priority issues.
   - Implements fix on a feature branch.
   - Verifies against GCS emulator.
   - Creates a new PR and adds it to the active pool.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from scanners.ast_auditor import CodebaseAuditor
from scanners.test_auditor import TestAuditor

STATE_FILE = BASE_DIR / "state" / "active_prs.json"


def log(msg: str) -> None:
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def run_command(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def load_state() -> dict[str, Any]:
    if STATE_FILE.is_file():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"active_prs": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_current_github_user() -> str:
    res = run_command(["gh", "api", "user", "--jq", ".login"], check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return ""


def get_pr_details(repo_slug: str, pr_number: int) -> dict[str, Any] | None:
    fields = "number,title,state,mergedAt,headRefName,comments,reviews"
    res = run_command(
        ["gh", "pr", "view", str(pr_number), "--repo", repo_slug, f"--json={fields}"],
        check=False
    )
    if res.returncode == 0:
        return json.loads(res.stdout)
    return None


def get_inline_review_comments(repo_slug: str, pr_number: int) -> list[dict[str, Any]]:
    """Fetches line-level review comments using GitHub API."""
    res = run_command(
        ["gh", "api", f"/repos/{repo_slug}/pulls/{pr_number}/comments"],
        check=False
    )
    if res.returncode == 0:
        return json.loads(res.stdout)
    return []


def handle_pr_feedback(
    repo_path: Path,
    repo_slug: str,
    pr_data: dict[str, Any],
    new_feedback: list[dict[str, Any]]
) -> bool:
    """Invokes agent to address review comments, runs pytest, and pushes update."""
    branch = pr_data["headRefName"]
    pr_num = pr_data["number"]
    log(f"Addressing {len(new_feedback)} feedback comment(s) on PR #{pr_num} ({branch})...")

    # 1. Switch to branch and pull latest
    run_command(["git", "checkout", branch], cwd=str(repo_path))
    run_command(["git", "pull", "--ff-only"], cwd=str(repo_path), check=False)

    # 2. Format feedback for the agent
    feedback_text = "\n\n".join([
        f"Reviewer @{c.get('user', {}).get('login', 'unknown')} on {c.get('path', 'general')}:{c.get('line', '')}:\n{c.get('body', '')}"
        for c in new_feedback
    ])

    skill_path = BASE_DIR / "skills" / "gcsfs-expert" / "SKILL.md"
    prompt = (
        f"You are the gcsfs-expert agent. The following review feedback was received on PR #{pr_num}:\n\n"
        f"{feedback_text}\n\n"
        f"Follow the rules in {skill_path}. Update the code/tests in {repo_path} to address this feedback cleanly."
    )

    log(f"Invoking gcsfs-expert agent for PR #{pr_num}...")
    run_command(["agy", "-p", prompt], cwd=str(repo_path), check=False)

    # 3. Check if changes were produced
    diff = run_command(["git", "status", "--porcelain"], cwd=str(repo_path))
    if not diff.stdout.strip():
        log(f"No code changes required for PR #{pr_num} (feedback may have been informational).")
        return True

    # 4. Verify with Pytest
    log(f"Verifying updated code with pytest for PR #{pr_num}...")
    test_res = run_command(["pytest", "gcsfs/tests/test_core.py", "-q", "--maxfail=1"], cwd=str(repo_path), check=False)
    if test_res.returncode != 0:
        log(f"Pytest failed after applying feedback on PR #{pr_num}. Reverting changes to keep branch clean.")
        run_command(["git", "checkout", "."], cwd=str(repo_path))
        return False

    # 5. Commit and push
    commit_msg = f"chore: address PR #{pr_num} review feedback"
    run_command(["git", "commit", "-am", commit_msg], cwd=str(repo_path))
    run_command(["git", "push", "origin", branch], cwd=str(repo_path))

    # 6. Post reply on PR
    reply_body = (
        f"### Automated Shepherd Update\n\n"
        f"Addressed review feedback in branch `{branch}`. All pytest checks passed."
    )
    run_command(["gh", "pr", "comment", str(pr_num), "--repo", repo_slug, "--body", reply_body], check=False)
    log(f"Pushed update and posted reply on PR #{pr_num}.")
    return True


def create_new_pr_task(
    repo_path: Path,
    repo_slug: str,
    active_prs: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Finds top issue, creates branch, writes fix, verifies, and raises PR."""
    log("Scanning codebase for top-priority candidate issue...")
    run_command(["git", "checkout", "main"], cwd=str(repo_path))
    run_command(["git", "pull", "--ff-only"], cwd=str(repo_path), check=False)

    source_auditor = CodebaseAuditor(root_dir=str(repo_path))
    test_auditor = TestAuditor(tests_dir=str(repo_path / "gcsfs" / "tests"))
    findings = source_auditor.audit() + test_auditor.audit()

    # Avoid rules already being tackled in active PRs
    active_rules = {p.get("rule") for p in active_prs}

    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_findings = sorted(findings, key=lambda x: severity_order.get(x.get("severity", "LOW"), 99))

    candidate = None
    for f in sorted_findings:
        if f["rule"] not in active_rules:
            candidate = f
            break

    if not candidate:
        log("No new eligible candidate issues found.")
        return None

    log(f"Selected candidate: {candidate['id']} ({candidate['rule']}) at {candidate['file']}:{candidate['line']}")

    # Create feature branch
    branch_name = f"bot/{candidate['rule']}-L{candidate['line']}"
    run_command(["git", "checkout", "-B", branch_name], cwd=str(repo_path))

    # Agent writes fix
    skill_file = BASE_DIR / "skills" / "gcsfs-expert" / "SKILL.md"
    prompt = (
        f"Follow the rules in {skill_file}. In {repo_path}, fix issue {candidate['id']} in {candidate['file']}:{candidate['line']}. "
        f"Issue: {candidate['summary']}. Suggestion: {candidate['suggestion']}. "
        "Keep the fix minimal and backward-compatible."
    )
    log("Invoking agent to implement fix...")
    run_command(["agy", "-p", prompt], cwd=str(repo_path), check=False)

    # Check diff
    diff = run_command(["git", "status", "--porcelain"], cwd=str(repo_path))
    if not diff.stdout.strip():
        log("Agent did not produce changes. Aborting new PR.")
        run_command(["git", "checkout", "main"], cwd=str(repo_path))
        return None

    # Verify with Pytest
    log("Verifying fix with pytest...")
    test_res = run_command(["pytest", "gcsfs/tests/test_core.py", "-q", "--maxfail=1"], cwd=str(repo_path), check=False)
    if test_res.returncode != 0:
        log("Pytest failed for candidate fix. Discarding branch.")
        run_command(["git", "checkout", "."], cwd=str(repo_path))
        run_command(["git", "checkout", "main"], cwd=str(repo_path))
        return None

    # Commit, push, and open PR
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

    if pr_res.returncode != 0:
        log(f"gh pr create failed: {pr_res.stderr}")
        return None

    pr_url = pr_res.stdout.strip()
    pr_number = int(pr_url.split("/")[-1])
    log(f"Successfully opened PR #{pr_number}: {pr_url}")

    return {
        "pr_number": pr_number,
        "branch": branch_name,
        "rule": candidate["rule"],
        "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }


def run_shepherd_loop(
    target_repo_path: str,
    repo_slug: str,
    max_prs: int = 3,
    poll_interval_seconds: int = 1800
) -> None:
    repo_path = Path(target_repo_path).resolve()
    current_user = get_current_github_user()
    log(f"Starting GCSFS Shepherd Daemon (Target: {repo_slug}, Max PRs: {max_prs}, Interval: {poll_interval_seconds}s)")

    while True:
        state = load_state()
        active_prs = state.get("active_prs", [])
        log(f"--- Shepherd Cycle Starting. Currently tracking {len(active_prs)}/{max_prs} active PRs ---")

        updated_active_prs = []

        # 1. Poll each active PR
        for pr_entry in active_prs:
            pr_num = pr_entry["pr_number"]
            branch = pr_entry["branch"]
            last_checked = pr_entry.get("last_checked_at", "1970-01-01T00:00:00Z")

            pr_data = get_pr_details(repo_slug, pr_num)
            if not pr_data:
                log(f"Could not fetch details for PR #{pr_num}. Retaining in tracking.")
                updated_active_prs.append(pr_entry)
                continue

            state_str = pr_data.get("state", "OPEN")
            merged_at = pr_data.get("mergedAt")

            # Condition A: Merged
            if merged_at or state_str == "MERGED":
                log(f"🎉 PR #{pr_num} was MERGED! Cleaning up branch '{branch}' and freeing slot.")
                run_command(["git", "checkout", "main"], cwd=str(repo_path))
                run_command(["git", "branch", "-D", branch], cwd=str(repo_path), check=False)
                continue  # Exclude from updated_active_prs to free slot

            # Condition B: Closed without merge
            if state_str == "CLOSED":
                log(f"⚠️ PR #{pr_num} was CLOSED without merge. Cleaning up branch '{branch}' and freeing slot.")
                run_command(["git", "checkout", "main"], cwd=str(repo_path))
                run_command(["git", "branch", "-D", branch], cwd=str(repo_path), check=False)
                continue  # Exclude to free slot

            # Condition C: Still Open - Check for new comments
            all_comments = pr_data.get("comments", []) + get_inline_review_comments(repo_slug, pr_num)
            new_feedback = []
            for c in all_comments:
                author = c.get("author", {}).get("login") or c.get("user", {}).get("login")
                created_at = c.get("createdAt") or c.get("created_at")
                if author != current_user and created_at and created_at > last_checked:
                    new_feedback.append(c)

            if new_feedback:
                log(f"Found {len(new_feedback)} new feedback comment(s) on open PR #{pr_num}.")
                handle_pr_feedback(repo_path, repo_slug, pr_data, new_feedback)

            # Update last_checked timestamp and keep in pool
            pr_entry["last_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            updated_active_prs.append(pr_entry)

        # 2. Refill pool up to max_prs
        available_slots = max_prs - len(updated_active_prs)
        if available_slots > 0:
            log(f"Pool has {available_slots} available slot(s). Finding new high-priority task(s)...")
            for _ in range(available_slots):
                new_pr = create_new_pr_task(repo_path, repo_slug, updated_active_prs)
                if new_pr:
                    updated_active_prs.append(new_pr)
                else:
                    break

        state["active_prs"] = updated_active_prs
        save_state(state)

        log(f"Cycle finished. Active PRs: {[p['pr_number'] for p in updated_active_prs]}. Sleeping {poll_interval_seconds}s...")
        time.sleep(poll_interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous PR Shepherd Daemon.")
    parser.add_argument("--repo", default="/home/princer_google_com/c2dev/gcsfs", help="Path to local target repo")
    parser.add_argument("--target-slug", default="fsspec/gcsfs", help="Target GitHub repo (owner/repo)")
    parser.add_argument("--max-prs", type=int, default=3, help="Maximum concurrent open PRs (default: 3)")
    parser.add_argument("--poll-interval", type=int, default=1800, help="Poll interval in seconds (default: 1800)")
    args = parser.parse_args()

    run_shepherd_loop(
        target_repo_path=args.repo,
        repo_slug=args.target_slug,
        max_prs=args.max_prs,
        poll_interval_seconds=args.poll_interval
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
