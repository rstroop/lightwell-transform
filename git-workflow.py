"""Git workflow helpers for Lightwell Transform.

Handles cloning, branching, committing, pushing, forking, and PR creation
so the skill file stays cross-platform and compact.

Usage:
    python git-workflow.py clone <github-url> [--target-dir <dir>]
    python git-workflow.py commit <repo-path> [--message <msg>]
    python git-workflow.py pushpr <repo-path> --repo-name <org/repo> [--body <text>]
"""

import argparse
import json
import os
import re
import subprocess
import sys


BRANCH = "lightwell-remediation-updates"
DEFAULT_MSG = "Update dependencies to Lightwell remediated versions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(args, check=False):
    """Run a command.  Returns (stdout, stderr, returncode)."""
    p = subprocess.run(
        args, capture_output=True, text=True, timeout=300
    )
    if check and p.returncode != 0:
        _fail(f"Command failed: {' '.join(args)}\n{p.stderr}")
    return p.stdout.strip(), p.stderr.strip(), p.returncode


def _tool_ok(name):
    """Return True if <name> --version succeeds."""
    _, _, rc = _run([name, "--version"])
    return rc == 0


def _fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _repo_name_from_url(url):
    """Extract bare repo name from a GitHub URL."""
    m = re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return m.group(1) if m else url.split("/")[-1]


# ---------------------------------------------------------------------------
# Action: clone / update
# ---------------------------------------------------------------------------

def do_clone(url, target_dir="lightwell-clones"):
    """Clone or fetch+reset a GitHub repo."""
    if not _tool_ok("git"):
        _fail("Git is not installed")

    name = _repo_name_from_url(url)
    repo_path = os.path.join(target_dir, name)
    git_path = os.path.join(repo_path, ".git")

    if os.path.isdir(git_path):
        _run(["git", "-C", repo_path, "fetch", "--all"])
        _run(["git", "-C", repo_path, "reset", "--hard", "origin/main"])
        print(json.dumps({"action": "updated", "repo": name, "path": repo_path}))
    else:
        os.makedirs(target_dir, exist_ok=True)
        out, err, rc = _run(["git", "clone", url, repo_path])
        if rc != 0:
            _fail(f"Clone failed: {err}")
        print(json.dumps({"action": "cloned", "repo": name, "path": repo_path}))


# ---------------------------------------------------------------------------
# Action: branch + commit
# ---------------------------------------------------------------------------

def do_commit(repo_path, message=DEFAULT_MSG):
    """Create/reset branch and commit changes."""
    if not _tool_ok("git"):
        _fail("Git is not installed")

    # Reset / create branch from origin/main
    _run(["git", "-C", repo_path, "checkout", "-B", BRANCH, "origin/main"])

    stdout, stderr, rc = _run([
        "git", "-C", repo_path, "config", "--global", "user.name"
    ])
    ename = not stdout
    stdout, stderr, rc = _run([
        "git", "-C", repo_path, "config", "--global", "user.email"
    ])
    email = not stdout

    if ename or email:
        print("WARNING: git identity not configured. Please set before proceeding.")
        sys.exit(1)

    _run(["git", "-C", repo_path, "add", "-A"])

    _, _, rcq = _run(["git", "-C", repo_path, "diff", "--cached", "--quiet"])
    if rcq == 0:
        print(json.dumps({"committed": False, "reason": "nothing-changed"}))
        return

    out, err, rc = _run(["git", "-C", repo_path, "commit", "-m", message])
    if rc != 0:
        _fail(f"Commit failed: {err}")
    print(json.dumps({"committed": True}))


# ---------------------------------------------------------------------------
# Action: push (+fork) + PR
# ---------------------------------------------------------------------------

def _whoami():
    """Return gh username."""
    out, _, rc = _run(["gh", "api", "user", "--jq", ".login"])
    return out if rc == 0 else ""


def _parse_json(raw):
    """Safely parse raw JSON into a Python object, fallback to None."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _list_prs(repo_name, head_ref=None):
    """Return list of PR dicts or empty list."""
    args = [
        "gh", "pr", "list",
        "--repo", repo_name,
        "--json", "url,number,title,state"
    ]
    if head_ref:
        args.extend(["--head", head_ref])
    out, _, _ = _run(args)
    data = _parse_json(out)
    return data if isinstance(data, list) else []


def _find_existing_pr(repo_name, me):
    """Try to find an existing PR from our branch. Returns dict or None."""
    candidates = _list_prs(repo_name, head_ref=f"{me}:{BRANCH}")
    if candidates:
        return candidates[0]
    # Fallback: match all open PRs by branch name in URL or title
    all_open = _list_prs(repo_name)[:]
    for pr in all_open:
        if pr.get("state") == "OPEN":
            if BRANCH in pr.get("url", "") or DEFAULT_MSG in pr.get("title", ""):
                return pr
    return None


def do_push_pr(repo_path, repo_name, body=None):
    """Push branch. If origin not writable, fork + re-point. Create or update PR."""
    if not _tool_ok("git") or not _tool_ok("gh"):
        _fail("Both git and gh are required for push+PR workflow")

    me = _whoami()
    body_val = (body or DEFAULT_MSG).replace("\\n", "\n")

    # Attempt push
    print(f"Pushing {BRANCH} to origin...")
    out_push, err_push, rc = _run([
        "git", "-C", repo_path, "push", "origin", BRANCH, "--force-with-lease"
    ])

    if rc != 0:
        print(f"Push denied (rc={rc}): {err_push}")
        # Fork and re-point origin
        print("Forking repo...")
        out_fork, err_fork, rc_fork = _run([
            "gh", "repo", "fork", repo_name, "--clone=false"
        ])
        if rc_fork != 0:
            _fail(f"Fork failed: {err_fork}")

        fork_url = out_fork.strip()
        print(f"Fork created at: {fork_url}")

        _run(["git", "-C", repo_path, "remote", "set-url", "origin", fork_url])
        # Fetch fresh refs before push
        _, fetch_err, fetch_rc = _run(["git", "-C", repo_path, "fetch", "origin"])
        if fetch_rc != 0:
            print(f"Fetch failed: {fetch_err}")

        out_push2, err_push2, rc2 = _run([
            "git", "-C", repo_path, "push", "origin", BRANCH, "--force-with-lease"
        ])
        if rc2 != 0:
            print(f"Push failed after fork (rc={rc2}): {err_push2}")
        else:
            if out_push2:
                print(out_push2)
            print("Push successful after fork.")
    else:
        if out_push:
            print(out_push)
        print("Push successful.")

    # Look for existing PR
    existing_pr = _find_existing_pr(repo_name, me)

    if existing_pr:
        print("\nPR already exists; updating body...")
        pr_num = str(existing_pr.get("number", ""))
        _run([
            "gh", "pr", "edit", pr_num,
            "--body", body_val,
            "--repo", repo_name,
        ], check=False)
        print(f"  PR #{existing_pr.get('number')} updated.")
    else:
        print("Creating PR...")
        out_create, err_create, rc_create = _run([
            "gh", "pr", "create",
            "--base", "main",
            "--head", f"{me}:{BRANCH}",
            "--title", DEFAULT_MSG,
            "--body", body_val,
            "--repo", repo_name,
        ])
        if rc_create != 0:
            # gh reports "already exists" with the URL in stderr — extract it
            m = re.search(r"https://github.com/[^/\s]+/[^/\s]+/pull/\d+", err_create)
            if m:
                existing_url = m.group(0)
                print(f"\nPR already exists at: {existing_url}")
                # Update body on that PR
                pr_num = existing_url.split("/")[-1]
                _run([
                    "gh", "pr", "edit", pr_num,
                    "--body", body_val,
                    "--repo", repo_name,
                ], check=False)
            else:
                print(f"PR creation failed (rc={rc_create}): {err_create}")
        else:
            if out_create:
                print(out_create)

    # Final PR summary — fetch all open PRs and find ours
    print("\nPR summary:")
    open_prs = _list_prs(repo_name)
    found = None
    for pr in open_prs:
        if pr.get("state") == "OPEN" and BRANCH in (pr.get("url", "")):
            found = pr
            break
    # If URL doesn't contain branch, try matching by title
    if not found:
        for pr in open_prs:
            if pr.get("state") == "OPEN" and DEFAULT_MSG in (pr.get("title", "")):
                found = pr
                break

    if found:
        print(f"  Number: {found.get('number')}")
        print(f"  Title:  {found.get('title')}")
        print(f"  State:  {found.get('state')}")
        print(f"  URL:    {found.get('url')}")
    else:
        print("  (no matching PR found)")


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Lightwell git workflow helpers")
    sub = parser.add_subparsers(dest="action", required=True)

    # clone
    p_clone = sub.add_parser("clone")
    p_clone.add_argument("url")
    p_clone.add_argument("--target-dir", default="lightwell-clones")

    # commit
    p_commit = sub.add_parser("commit")
    p_commit.add_argument("repo_path")
    p_commit.add_argument("--message", default=DEFAULT_MSG)

    # pushpr
    p_pushpr = sub.add_parser("pushpr")
    p_pushpr.add_argument("repo_path")
    p_pushpr.add_argument("--repo-name", required=True)
    p_pushpr.add_argument("--body", default=None)

    args = parser.parse_args()

    if args.action == "clone":
        do_clone(args.url, args.target_dir)
    elif args.action == "commit":
        do_commit(args.repo_path, args.message)
    elif args.action == "pushpr":
        do_push_pr(args.repo_path, args.repo_name, args.body)


if __name__ == "__main__":
    main()