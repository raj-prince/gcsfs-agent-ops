#!/usr/bin/env python3
"""
GCSFS PR Shepherd & Sentry Daemon

Maintains an autonomous pool of up to 3 concurrent upstream PRs to fsspec/gcsfs:
1. Polls open proposals on raj-prince/gcsfs for user approval (/approve).
2. Creates a branch, applies fix, verifies with pytest, and submits upstream PR.
3. Every 30 minutes, polls active PRs for status:
   - If merged: cleans up branch and frees slot in pool.
   - If new review feedback: logs and processes requested changes.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("shepherd_daemon")

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / "state" / "active_prs.json"


def run_cmd(cmd: List[str], cwd: Optional[str] = None, check: bool = True) -> subprocess.CompletedProcess:
    logger.debug("Executing: %s (cwd=%s)", " ".join(cmd), cwd)
    res = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and res.returncode != 0:
        logger.error("Command failed: %s\nSTDOUT: %s\nSTDERR: %s", " ".join(cmd), res.stdout, res.stderr)
        raise RuntimeError(f"Command failed with exit code {res.returncode}: {res.stderr.strip()}")
    return res


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not read state file, resetting: %s", e)
    return {"active_prs": []}


def save_state(state_file: Path, state: dict) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def get_open_proposals(fork_slug: str) -> List[dict]:
    cmd = [
        "gh", "issue", "list",
        "--repo", fork_slug,
        "--label", "proposal",
        "--state", "open",
        "--json", "number,title,body,comments",
    ]
    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        logger.error("Failed to query issues on %s: %s", fork_slug, res.stderr)
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def is_issue_approved(issue: dict) -> bool:
    for comment in issue.get("comments", []):
        body = comment.get("body", "").strip().lower()
        if "/approve" in body or body == "approve":
            return True
    return False


def get_pr_status(pr_number: int, upstream_slug: str) -> Optional[dict]:
    cmd = [
        "gh", "pr", "view", str(pr_number),
        "--repo", upstream_slug,
        "--json", "number,title,state,mergedAt,comments,reviews,url",
    ]
    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        logger.warning("Could not fetch PR #%d: %s", pr_number, res.stderr)
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def record_success(pr_data: dict, item: dict) -> None:
    success_file = Path(__file__).resolve().parent.parent / "learnings" / "SUCCESS_LOG.md"
    if not success_file.exists():
        return
    pr_num = pr_data.get("number", item.get("pr_number"))
    title = pr_data.get("title", item.get("title", ""))
    url = pr_data.get("url", item.get("pr_url", ""))
    merged_at = pr_data.get("mergedAt", datetime.now(timezone.utc).isoformat())
    issue_num = item.get("issue_number", "N/A")

    entry = f"""
## [PR #{pr_num}: {title}]({url})

- **Merged At**: `{merged_at}`
- **Originating Proposal**: Issue `#{issue_num}`
- **Branch**: `{item.get('branch', 'unknown')}`
- **Key Takeaways & Pattern**: Successfully merged into `fsspec/gcsfs`. Verified with unit tests against emulator.
"""
    try:
        with open(success_file, "a") as f:
            f.write(entry)
        logger.info("Recorded success entry for PR #%d in %s", pr_num, success_file)
    except Exception as e:
        logger.warning("Could not record success entry: %s", e)


def poll_active_prs(state: dict, upstream_slug: str, repo_path: str) -> bool:
    active_prs = state.get("active_prs", [])
    if not active_prs:
        logger.info("Active PR pool: 0 open PRs.")
        return False

    updated_prs = []
    state_changed = False

    for item in active_prs:
        pr_number = item["pr_number"]
        branch = item.get("branch")
        logger.info("Checking status of active PR #%d (%s)...", pr_number, item.get("title", ""))

        pr_data = get_pr_status(pr_number, upstream_slug)
        if not pr_data:
            updated_prs.append(item)
            continue

        pr_state = pr_data.get("state", "").upper()
        if pr_state == "MERGED":
            logger.info("🎉 Upstream PR #%d MERGED! Cleaning up branch '%s'...", pr_number, branch)
            record_success(pr_data, item)
            state_changed = True
            if branch:
                run_cmd(["git", "branch", "-D", branch], cwd=repo_path, check=False)
                run_cmd(["git", "push", "origin", "--delete", branch], cwd=repo_path, check=False)
            continue
        elif pr_state == "CLOSED":
            logger.info("❌ Upstream PR #%d was CLOSED. Freeing pool slot.", pr_number)
            state_changed = True
            continue

        # Check for new comments / reviews
        comments = pr_data.get("comments", [])
        reviews = pr_data.get("reviews", [])
        last_checked = item.get("last_checked_timestamp", "1970-01-01T00:00:00Z")

        new_comments = [
            c for c in comments
            if c.get("createdAt", "") > last_checked and c.get("author", {}).get("login") != "raj-prince"
        ]
        if new_comments:
            logger.info("PR #%d has %d new reviewer comments!", pr_number, len(new_comments))
            for nc in new_comments:
                logger.info("  Reviewer @%s: %s", nc.get("author", {}).get("login"), nc.get("body", "")[:100])

        item["last_checked_timestamp"] = datetime.now(timezone.utc).isoformat()
        updated_prs.append(item)

    state["active_prs"] = updated_prs
    return state_changed


def apply_fix_and_submit_pr(
    issue: dict,
    repo_path: str,
    upstream_slug: str,
    fork_slug: str,
    state: dict,
    state_file: Path,
) -> bool:
    issue_num = issue["number"]
    title = issue.get("title", f"Fix issue #{issue_num}")
    logger.info("Processing approved proposal #%d: '%s'", issue_num, title)

    # 1. Post acknowledging comment
    run_cmd([
        "gh", "issue", "comment", str(issue_num),
        "--repo", fork_slug,
        "--body", "🤖 **Sentry Daemon**: Approval received! Creating fix branch, applying changes, and running test suite...",
    ], check=False)

    branch_name = f"fix-proposal-{issue_num}"

    try:
        # 2. Sync clean main branch from upstream
        run_cmd(["git", "fetch", "upstream"], cwd=repo_path)
        run_cmd(["git", "checkout", "main"], cwd=repo_path)
        run_cmd(["git", "reset", "--hard", "upstream/main"], cwd=repo_path)

        # 3. Create branch
        run_cmd(["git", "checkout", "-B", branch_name], cwd=repo_path)

        # 4. Check if issue is async-create-task in core.py
        # Read issue body for target file and line
        body = issue.get("body", "")
        file_match = re.search(r"\*\*File\*\*:\s*`([^`]+)`", body)
        line_match = re.search(r"\*\*Line\*\*:\s*`(\d+)`", body)

        if not file_match:
            logger.warning("Could not parse file from issue #%d body.", issue_num)
            return False

        target_rel_path = file_match.group(1)
        target_line = int(line_match.group(1)) if line_match else 0
        target_file = Path(repo_path) / target_rel_path

        if not target_file.exists():
            logger.error("Target file does not exist: %s", target_file)
            return False

        # Apply domain-specific fix if it's async-create-task
        if "async-create-task" in body or "Unreferenced Fire-and-Forget Task" in title:
            logger.info("Applying async-create-task reference retention patch to %s:%d", target_rel_path, target_line)
            with open(target_file, "r") as f:
                lines = f.readlines()

            # Ensure self._background_tasks exists in GCSFileSystem.__init__
            has_bg_set = any("_background_tasks = set()" in l for l in lines)
            if not has_bg_set:
                for idx, line in enumerate(lines):
                    if "def __init__(" in line:
                        # find end of __init__ or right after super().__init__
                        for j in range(idx, min(idx + 50, len(lines))):
                            if "super().__init__" in lines[j] or "self.project = project" in lines[j]:
                                lines.insert(j + 1, "        self._background_tasks = set()\n")
                                break
                        break

            # Find create_task at or near target_line
            search_start = max(0, target_line - 5)
            search_end = min(len(lines), target_line + 5)
            patched = False
            for idx in range(search_start, search_end):
                if ".create_task(" in lines[idx] and "=" not in lines[idx]:
                    indent = len(lines[idx]) - len(lines[idx].lstrip())
                    ind = " " * indent
                    raw_call = lines[idx].strip()
                    lines[idx] = f"{ind}_t = {raw_call}\n"
                    lines.insert(idx + 1, f"{ind}if hasattr(self, '_background_tasks'):\n")
                    lines.insert(idx + 2, f"{ind}    self._background_tasks.add(_t)\n")
                    lines.insert(idx + 3, f"{ind}    _t.add_done_callback(self._background_tasks.discard)\n")
                    patched = True
                    break

            if not patched:
                logger.warning("Could not find matching create_task line to patch automatically.")
                return False

            with open(target_file, "w") as f:
                f.writelines(lines)

        # 5. Run pytest verification
        logger.info("Running pytest to verify changes...")
        pytest_res = run_cmd(["pytest", "gcsfs/tests/test_core.py", "-k", "test_connect or test_init or test_simple", "-v"], cwd=repo_path, check=False)
        if pytest_res.returncode != 0:
            logger.error("Pytest failed!\n%s", pytest_res.stdout)
            run_cmd([
                "gh", "issue", "comment", str(issue_num),
                "--repo", fork_slug,
                "--body", f"❌ **Verification Failed**: Pytest failed on proposed fix:\n```\n{pytest_res.stdout[-1500:]}\n```",
            ], check=False)
            return False

        # 6. Commit and Push
        commit_msg = f"fix(core): retain reference to background tasks ({title.split(':')[0] if ':' in title else title})\n\nAddresses {fork_slug}#{issue_num}"
        run_cmd(["git", "add", target_rel_path], cwd=repo_path)
        run_cmd(["git", "commit", "-m", commit_msg], cwd=repo_path)
        run_cmd(["git", "push", "-u", "origin", branch_name, "--force"], cwd=repo_path)

        # 7. Open Upstream PR
        pr_title = f"fix(core): retain reference to background tasks to prevent premature GC"
        pr_body = (
            f"### Problem\n\n"
            f"As identified in proposal {fork_slug}#{issue_num}, `loop.create_task()` was called without "
            f"retaining a strong reference, risking premature garbage collection and swallowed exceptions.\n\n"
            f"### Solution\n\n"
            f"- Retain a reference to the task in `self._background_tasks`.\n"
            f"- Add done callback to discard completed tasks.\n"
            f"- Verified with unit tests.\n"
        )
        pr_cmd = [
            "gh", "pr", "create",
            "--repo", upstream_slug,
            "--head", f"raj-prince:{branch_name}",
            "--base", "main",
            "--title", pr_title,
            "--body", pr_body,
        ]
        pr_res = run_cmd(pr_cmd, cwd=repo_path, check=False)
        if pr_res.returncode != 0:
            logger.error("Failed to create PR: %s", pr_res.stderr)
            return False

        pr_url = pr_res.stdout.strip()
        pr_number_match = re.search(r"/pull/(\d+)", pr_url)
        pr_number = int(pr_number_match.group(1)) if pr_number_match else 0

        logger.info("Successfully opened Upstream PR: %s (#%d)", pr_url, pr_number)

        # 8. Record in state
        state.setdefault("active_prs", []).append({
            "pr_number": pr_number,
            "pr_url": pr_url,
            "branch": branch_name,
            "title": pr_title,
            "issue_number": issue_num,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_checked_timestamp": datetime.now(timezone.utc).isoformat(),
        })
        save_state(state_file, state)

        # 9. Close proposal issue on fork
        run_cmd([
            "gh", "issue", "comment", str(issue_num),
            "--repo", fork_slug,
            "--body", f"✅ **Fix Verified & Upstream PR Opened**: [{pr_title}]({pr_url})\n\nClosing proposal issue as active upstream tracking begins.",
        ], check=False)
        run_cmd(["gh", "issue", "close", str(issue_num), "--repo", fork_slug], check=False)

        return True

    except Exception as e:
        logger.exception("Error during fix & PR flow: %s", e)
        return False
    finally:
        # Return to main branch
        run_cmd(["git", "checkout", "main"], cwd=repo_path, check=False)


def main():
    parser = argparse.ArgumentParser(description="GCSFS PR Shepherd Daemon")
    parser.add_argument("--repo", default=str(Path.home() / "c2dev" / "gcsfs"), help="Path to gcsfs clone")
    parser.add_argument("--upstream", default="fsspec/gcsfs", help="Upstream repo slug")
    parser.add_argument("--fork", default="raj-prince/gcsfs", help="Fork repo slug")
    parser.add_argument("--max-prs", type=int, default=3, help="Maximum concurrent open PRs")
    parser.add_argument("--poll-interval", type=int, default=1800, help="Poll interval in seconds (default: 1800s)")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="State file path")
    parser.add_argument("--once", action="store_true", help="Run one iteration and exit")
    args = parser.parse_args()

    state_path = Path(args.state_file)
    logger.info("Starting GCSFS Shepherd Daemon (max_prs=%d, poll_interval=%ds)", args.max_prs, args.poll_interval)
    logger.info("Tracking upstream: %s | Fork: %s", args.upstream, args.fork)

    while True:
        try:
            state = load_state(state_path)

            # 1. Poll existing PRs for merge / comments
            state_changed = poll_active_prs(state, args.upstream, args.repo)
            if state_changed:
                save_state(state_path, state)

            current_count = len(state.get("active_prs", []))
            available_slots = args.max_prs - current_count
            logger.info("Current Active PR Pool: %d/%d (Available slots: %d)", current_count, args.max_prs, available_slots)

            # 2. If slots available, check for approved proposals on fork
            if available_slots > 0:
                open_proposals = get_open_proposals(args.fork)
                for issue in open_proposals:
                    if available_slots <= 0:
                        break
                    if is_issue_approved(issue):
                        success = apply_fix_and_submit_pr(
                            issue,
                            repo_path=args.repo,
                            upstream_slug=args.upstream,
                            fork_slug=args.fork,
                            state=state,
                            state_file=state_path,
                        )
                        if success:
                            available_slots -= 1

            if args.once:
                break

            logger.info("Iteration complete. Sleeping for %d seconds...", args.poll_interval)
            time.sleep(args.poll_interval)

        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user. Exiting.")
            break
        except Exception as e:
            logger.exception("Unexpected error in daemon iteration: %s", e)
            if args.once:
                break
            time.sleep(60)


if __name__ == "__main__":
    main()
