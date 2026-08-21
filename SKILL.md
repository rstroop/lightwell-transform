---
name: lightwell-transform
description: >-
  Audits Maven POM dependencies against the Project Lightwell remediated-Java catalog.
  Parses POM files in a folder or git repo, compares each dependency to the lightwell catalog,
  and reports status: exact match, needs update, or base-version mismatch.
  Manages the git workflow for updating a repository with new dependencies.
version: "0.0.1"
---

# Lightwell Transform Skill

## Critical execution rules — READ FIRST

**Execute ALL steps continuously without pausing.** Do NOT present intermediate summaries, plans, status updates, or ask for input. Run every step back-to-back in a single turn. **Only pause for explicitly required user questions:**

1. **Git identity missing (Step 0):** If `git config --global user.name` or `user.email` is empty, **STOP and ask** for both before doing anything else.
2. **Scope confirmation (Step 4):** Present which scopes to update and wait for the user's choice.
3. **Catalog stale:** If the catalog fails validation, pause for the user to provide a new one.

Everything else — prerequisite checks, cloning, verifying catalog, running audit, applying edits, re-running audit, branching, committing — must happen without stopping. Do not use todo lists or present plans. Just **do it**.

Given a directory containing one or more Maven `pom.xml` files and a local `catalog.json`, produce a full audit report identifying every dependency's alignment with Project Lightwell remediated versions. The `catalog.json` cannot be downloaded automatically nor ship with the skill — it must be added by the user and excluded from version control (add to `.gitignore`).

## What the POM compare script does

See `pom-compare.py` module docstring for details on parsing, property resolution, classification, and catalog matching logic.

## Prerequisites

**Required for all workflows:**
- **Python 3.7+** (stdlib only; no extra packages)
- `catalog.json` on disk, ideally in the skill directory (`<skill>/catalog.json`)

**Optional**
- **Git** / **GitHub CLI**

### Verifying prerequisites

Use `--version` to confirm tools exist (do NOT use `which`, `command -v`, or bare `where`). If git or gh are missing, ask the user if they want to install them — offer links (Git: https://git-scm.com/downloads, gh: https://cli.github.com/) and remind them to run `gh auth login` after installing.

```powershell
& python --version; & git --version; & gh --version
```
```bash
python3 --version; git --version; gh --version
```

Before committing, ensure global Git identity is set (this skill operates across many repos, so always check `--global`, not per-repo):

```bash
git config --global user.name
git config --global user.email
```

If either is empty, ask the user for their name and email and set both globally.

## Unified Workflow

### Entry point — detect source

**GitHub URL provided:** If Git or gh are unavailable, ask the user if they want to install them and continue.
**Local folder path (or omitted):** Audit in-place. Only Python is required; Git/gh are optional.

> **IMPORTANT:** Always use the clone target-dir `lightwell-clones` when given a GitHub URL. Do NOT clone into a bare repo. If you notice a misplaced clone (e.g. a `<repo-name>/` directory outside `lightwell-clones/`), ignore it and work only from the correct location.

### Step 1: Prerequisites + catalog

Run the prerequisite check above. Then ensure `catalog.json` exists in the skill directory. If missing, search workspace root and project directory. If found elsewhere, offer to copy it to the skill directory so it persists across future audits. **Never skip this step.**

### Step 2: Clone (GitHub URLs only)

Skip for local folders. Run:

```bash
python git-workflow.py clone <github-url> --target-dir lightwell-clones
```

See `git-workflow.py` docstring for details on clone vs. fetch+reset behavior. The script outputs JSON to the console; parse it to get the resolved `<repo-path>` for subsequent steps.

### Step 3: Run audit

For **GitHub URLs**, set `<target-dir>` to `lightwell-clones/<repo-name>`. Do NOT use a bare `<repo-name>/` path. For local folders, use user-supplied path or `.`. Run:

```powershell
# Windows
python pom-compare.py <target-dir> <skill-path>\catalog.json
```
```bash
# Linux / macOS
python3 pom-compare.py <target-dir> <skill-path>/catalog.json
```

**Do not stop until the audit returns results.** If catalog is stale, open a browser page at https://lightwell-console-lightwell.apps.virt.na-launch.com/api/catalog to have the user download a new one, replace the old one, and re-run. The catalog is behind an authorization wall, do not download it automatically, have the user download it and verify it is valid json before replacing the stale copy.

### Step 4: Scope confirmation

Present actionable updates grouped by scope and wait for an explicit choice. **Never default to "all":**

> "There are actionable updates in these scopes:
> - Default properties (always active): X items
> - Profile 'ci-community' overrides: Y items
> - Everything (combined): all items
> - Nothing: leave as-is
> Which would you like me to update?"

### Step 5: Apply changes + re-audit

Edit only the `<properties>` values for the chosen scope across the requested `pom.xml` files. Respect user constraints (e.g. skip parent POMs, skip certain profiles). **Always re-run the audit after applying changes** to confirm resolution. If items remain, ask if user wants further changes.

### Step 6: Commit (with approval)

**Always ask before committing.** Only proceed with explicit yes. If this is not a Git project, stop here. Run:

```bash
python git-workflow.py commit <repo-path>
```

The script handles branch creation, identity check, staging, and conditional commit. See `git-workflow.py` docstring for details.

### Step 7: Push + PR (with approval)

**Always ask before pushing or creating a PR.** Skip for non-Git projects. If gh is unavailable instruct the user on how to manually create a pull request. Run:

```bash
python git-workflow.py pushpr <repo-path> --repo-name "<owner>/<repo>" --body "<markdown-summary>"
```

The script handles push, fork fallback, and PR creation/update. Use the audit results to build a concise `--body` summary. The script outputs JSON including the PR URL — return it to the user. See `git-workflow.py` docstring for details.
