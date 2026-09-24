#!/usr/bin/env python3
"""Autonomous PR Shepherd Daemon with Fork Proposal Approvals.

Workflow:
1. Polls active upstream PRs (fsspec/gcsfs) every 30 minutes (Max 3 concurrent PRs):
   - If MERGED: Cleans up branch, frees slot, skips!
   - If CLOSED: Cleans up branch, frees slot.
   - If OPEN: Inspects for reviewer comments, addresses them with tests, pushes updates.
2. If active PR count < 3:
   - Polls forked repository (raj-prince/gcsfs) for open proposal issues.
   - Checks if the owner commented `/approve` or `approve` on any proposal.
   - For approved proposals:
     * Branches, implements fix with gcsfs-expert agent.
     * Verifies with pytest against emulator.
     * Pushes to fork and opens upstream PR on fsspec/gcsfs.
     * Comments on and closes the proposal issue in the fork.
     * Tracks the new upstream PR in the 3-PR active pool.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

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
    return "raj-prince"


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
    branch = pr_data["headRefName"]
    pr_num = pr_data["number"]
    log(f"Addressing {len(new_feedback)} feedback comment(s) on PR #{pr_num} ({branch})...")

    run_command(["git", "checkout", branch], cwd=str(repo_path))
    run_command(["git", "pull", "--ff-only"], cwd=str(repo_path), check=False)

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

    diff = run_command(["git", "status", "--porcelain"], cwd=str(repo_path))
    if not diff.stdout.strip():
        log(f"No code changes required for PR #{pr_num}.")
        return True

    log(f"Verifying updated code with pytest for PR #{pr_num}...")
    test_res = run_command(["pytest", "gcsfs/tests/test_core.py", "-q", "--maxfail=1"], cwd=str(repo_path), check=False)
    if test_res.returncode != 0:
        log(f"Pytest failed after applying feedback on PR #{pr_num}. Reverting changes.")
        run_command(["git", "checkout", "."], cwd=str(repo_path))
        return False

    commit_msg = f"chore: address PR #{pr_num} review feedback"
    run_command(["git", "commit", "-am", commit_msg], cwd=str(repo_path))
    run_command(["git", "push", "origin", branch], cwd=str(repo_path))

    reply_body = f"### Automated Shepherd Update\n\nAddressed review feedback in branch `{branch}`. All pytest checks passed."
    run_command(["gh", "pr", "comment", str(pr_num), "--repo", repo_slug, "--body", reply_body], check=False)
    log(f"Pushed update and posted reply on PR #{pr_num}.")
    return True


def find_approved_proposals_in_fork(fork_slug: str, owner_user: str) -> list[dict[str, Any]]:
    """Checks fork issues for approval comments from owner."""
    res = run_command(
        ["gh", "issue", "list", "--repo", fork_slug, "--state", "open", "--json", "number,title,body,comments"],
        check=False
    )
    if res.returncode != 0:
        return []

    issues = json.loads(res.stdout)
    approved = []
    approval_pattern = re.compile(r"\b(/approve|approve|lgtm|go ahead)\b", re.IGNORECASE)

    for issue in issues:
        comments = issue.get("comments", [])
        for c in comments:
            author = c.get("author", {}).get("login")
            body = c.get("body", "")
            if author == owner_user and approval_pattern.search(body):
                approved.append(issue)
                break
    return approved


def process_approved_proposal(
    repo_path: Path,
    upstream_slug: str,
    fork_slug: str,
    issue: dict[str, Any]
) -> dict[str, Any] | None:
    issue_num = issue["number"]
    title = issue["title"]
    body = issue["body"]
    log(f"Processing approved proposal #{issue_num}: '{title}'...")

    # Post initial acknowledgement
    run_command([
        "gh", "issue", "comment", str(issue_num),
        "--repo", fork_slug,
        "--body", "🚀 **Approval received!** The gcsfs-expert daemon is creating a branch, implementing the fix, and running tests..."
    ], check=False)

    run_command(["git", "checkout", "main"], cwd=str(repo_path))
    run_command(["git", "pull", "--ff-only"], cwd=str(repo_path), check=False)

    branch_name = f"bot/proposal-{issue_num}"
    run_command(["git", "checkout", "-B", branch_name], cwd=str(repo_path))

    skill_file = BASE_DIR / "skills" / "gcsfs-expert" / "SKILL.md"
    prompt = (
        f"Follow the rules in {skill_file}. In workspace {repo_path}, implement the fix for proposal #{issue_num}:\n\n"
        f"Title: {title}\nDetails: {body}\n\n"
        "Ensure the fix is minimal, safe, and backwards-compatible with fsspec."
    )
    log(f"Invoking gcsfs-expert agent to fix proposal #{issue_num}...")
    run_command(["agy", "-p", prompt], cwd=str(repo_path), check=False)

    diff = run_command(["git", "status", "--porcelain"], cwd=str(repo_path))
    if not diff.stdout.strip():
        log(f"Agent did not generate changes for proposal #{issue_num}.")
        run_command(["gh", "issue", "comment", str(issue_num), "--repo", fork_slug, "--body", "⚠️ Agent could not generate a valid diff. Leaving issue open."], check=False)
        run_command(["git", "checkout", "main"], cwd=str(repo_path))
        return None

    log("Running pytest verification against GCS emulator...")
    test_res = run_command(["pytest", "gcsfs/tests/test_core.py", "-q", "--maxfail=1"], cwd=str(repo_path), check=False)
    if test_res.returncode != 0:
        log(f"Pytest failed for proposal #{issue_num}. Discarding.")
        run_command(["git", "checkout", "."], cwd=str(repo_path))
        run_command(["git", "checkout", "main"], cwd=str(repo_path))
        run_command(["gh", "issue", "comment", str(issue_num), "--repo", fork_slug, "--body", "❌ Tests failed against emulator. Aborting PR creation."], check=False)
        return None

    # Commit and push to fork
    commit_msg = f"fix: {title.replace('[Proposal] ', '')}"
    run_command(["git", "commit", "-am", commit_msg], cwd=str(repo_path))
    run_command(["git", "push", "origin", branch_name, "--force"], cwd=str(repo_path))

    # Open PR on upstream fsspec/gcsfs
    fork_user = fork_slug.split("/")[0]
    pr_head = f"{fork_user}:{branch_name}"
    pr_body = (
        f"## Automated Fix for Proposal #{issue_num}\n\n"
        f"{body}\n\n"
        f"### Verification\n- Tested against local GCS emulator: **PASSED**."
    )

    log(f"Submitting PR to {upstream_slug} with head {pr_head}...")
    pr_res = run_command([
        "gh", "pr", "create",
        "--repo", upstream_slug,
        "--title", commit_msg,
        "--body", pr_body,
        "--head", pr_head,
        "--base", "main"
    ], cwd=str(repo_path), check=False)

    if pr_res.returncode != 0:
        log(f"Failed to create PR: {pr_res.stderr}")
        return None

    pr_url = pr_res.stdout.strip()
    upstream_pr_num = int(pr_url.split("/")[-1])
    log(f"🎉 Successfully created upstream PR #{upstream_pr_num}: {pr_url}")

    # Close proposal issue in fork
    close_msg = f"✅ Upstream Pull Request created: {pr_url}\nClosing this proposal."
    run_command(["gh", "issue", "close", str(issue_num), "--repo", fork_slug, "--comment", close_msg], check=False)

    return {
        "pr_number": upstream_pr_num,
        "branch": branch_name,
        "rule": title,
        "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }


def run_shepherd_loop(
    target_repo_path: str,
    upstream_slug: str,
    fork_slug: str,
    max_prs: int = 3,
    poll_interval_seconds: int = 1800
) -> None:
    repo_path = Path(target_repo_path).resolve()
    current_user = get_current_github_user()
    log(f"Starting GCSFS Shepherd (Upstream: {upstream_slug}, Fork: {fork_slug}, Max PRs: {max_prs}, Interval: {poll_interval_seconds}s)")

    while True:
        state = load_state()
        active_prs = state.get("active_prs", [])
        log(f"--- Cycle Starting. Tracking {len(active_prs)}/{max_prs} active upstream PRs ---")

        updated_active_prs = []

        # 1. Poll each active upstream PR on fsspec/gcsfs
        for pr_entry in active_prs:
            pr_num = pr_entry["pr_number"]
            branch = pr_entry["branch"]
            last_checked = pr_entry.get("last_checked_at", "1970-01-01T00:00:00Z")

            pr_data = get_pr_details(upstream_slug, pr_num)
            if not pr_data:
                updated_active_prs.append(pr_entry)
                continue

            state_str = pr_data.get("state", "OPEN")
            merged_at = pr_data.get("mergedAt")

            # Condition A: Merged!
            if merged_at or state_str == "MERGED":
                log(f"🎉 Upstream PR #{pr_num} was MERGED! Cleaning up branch '{branch}' and freeing slot.")
                run_command(["git", "checkout", "main"], cwd=str(repo_path))
                run_command(["git", "branch", "-D", branch], cwd=str(repo_path), check=False)
                continue  # Skip/remove from active pool

            # Condition B: Closed without merge
            if state_str == "CLOSED":
                log(f"⚠️ Upstream PR #{pr_num} was CLOSED. Cleaning up branch '{branch}' and freeing slot.")
                run_command(["git", "checkout", "main"], cwd=str(repo_path))
                run_command(["git", "branch", "-D", branch], cwd=str(repo_path), check=False)
                continue

            # Condition C: Still Open -> Check for reviewer feedback
            all_comments = pr_data.get("comments", []) + get_inline_review_comments(upstream_slug, pr_num)
            new_feedback = []
            for c in all_comments:
                author = c.get("author", {}).get("login") or c.get("user", {}).get("login")
                created_at = c.get("createdAt") or c.get("created_at")
                if author != current_user and created_at and created_at > last_checked:
                    new_feedback.append(c)

            if new_feedback:
                log(f"Found {len(new_feedback)} feedback comment(s) on upstream PR #{pr_num}.")
                handle_pr_feedback(repo_path, upstream_slug, pr_data, new_feedback)

            pr_entry["last_checked_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            updated_active_prs.append(pr_entry)

        # 2. Check for approved proposal issues in your fork if pool has slots!
        available_slots = max_prs - len(updated_active_prs)
        if available_slots > 0:
            log(f"Pool has {available_slots} open slot(s). Checking {fork_slug}/issues for approved proposals...")
            approved_issues = find_approved_proposals_in_fork(fork_slug, owner_user=current_user)
            log(f"Found {len(approved_issues)} approved proposal issue(s).")

            for issue in approved_issues[:available_slots]:
                new_pr = process_approved_proposal(repo_path, upstream_slug, fork_slug, issue)
                if new_pr:
                    updated_active_prs.append(new_pr)

        state["active_prs"] = updated_active_prs
        save_state(state)

        log(f"Cycle complete. Active upstream PRs: {[p['pr_number'] for p in updated_active_prs]}. Sleeping {poll_interval_seconds}s...")
        time.sleep(poll_interval_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description="Autonomous Shepherd with Fork Proposal Approvals.")
    parser.add_argument("--repo", default="/home/princer_google_com/c2dev/gcsfs", help="Path to local target repo")
    parser.add_argument("--upstream", default="fsspec/gcsfs", help="Upstream repo (owner/repo)")
    parser.add_argument("--fork", default="raj-prince/gcsfs", help="Fork repo (owner/repo)")
    parser.add_argument("--max-prs", type=int, default=3, help="Max concurrent PRs (default: 3)")
    parser.add_argument("--poll-interval", type=int, default=1800, help="Poll interval in seconds (default: 1800)")
    args = parser.parse_args()

    run_shepherd_loop(
        target_repo_path=args.repo,
        upstream_slug=args.upstream,
        fork_slug=args.fork,
        max_prs=args.max_prs,
        poll_interval_seconds=args.poll_interval
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
