# GitHub Actions workflow migration

Bulk-migrate multiple GitHub repositories to **central workflow templates**. For each repo, the script clones **`main`**, **quarantines** existing workflows, copies template files plus **CODEOWNERS**, pushes a **feature branch**, and opens a **pull request** into **`main`**.

---

## What it does (per repo)

1. **Clone or refresh** the repo under `./migration-work/<owner>__<repo>/` (configurable via `--workdir-parent`).
2. **Quarantine** every file in `.github/workflows/` ending in `.yml` / `.yaml`: rename `foo.yml` → `foo-old.yml` and prefix the YAML top-level `name:` with **`Old `** (skipped if already prefixed).
3. **Copy workflow templates** from `--templates-dir` (default: this repo’s `workflows/`) into `.github/workflows/`. This repository ships **`workflow.yml`** as the single default template.
4. **Copy** `.github/CODEOWNERS` from `--codeowners-file` (default: `codeowners/CODEOWNERS`).
5. **Commit**, **push** the feature branch, and **create a PR** (`base`: `main`) via the GitHub REST API—or reuse an existing open PR for that head branch.

Typical migrated caller workflow invokes a shared reusable workflow (example in `workflows/workflow.yml`):

```yaml
uses: vsbopi/workflows/.github/workflows/workflow.yml@main
```

---

## Requirements

- **Python** 3.10+ (recommended; project uses contemporary typing/`Path` helpers).
- **Git** installed and available on `PATH`.
- Network access to **github.com**.
- A **personal access token** with permissions to clone, push branches, and open pull requests for the targets (typically **`repo`** for private repos; respect org SSO/PAT restrictions).

---

## Setup

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Authentication

Provide **`GITHUB_TOKEN`** or **`GH_TOKEN`** in your environment **or** in a `.env` file loaded by [`python-dotenv`](https://github.com/theskumar/python-dotenv).

| Variable           | Purpose                                      |
|--------------------|----------------------------------------------|
| `GITHUB_TOKEN`/`GH_TOKEN` | Push + GitHub REST API (create/find PR). |

Never commit `.env`. It is ignored by `.gitignore`.

---

## Repository layout

| Path | Purpose |
|------|---------|
| `migrate_workflows.py` | CLI entrypoint |
| `workflows/` | Default **`--templates-dir`**; shipped template `workflow.yml` |
| `codeowners/CODEOWNERS` | Default template copied to `.github/CODEOWNERS` in targets |
| `migration-work/` | Default clone directory (ignored by `.gitignore`) |

**Extending templates:** Only basenames listed in **`TEMPLATE_FILES`** inside `migrate_workflows.py` are copied. Put the actual files under the default `workflows/` tree or another path, and pass **`--templates-dir`** to that folder. To add more workflows, **append their filenames** to `TEMPLATE_FILES` and include those files in the directory you point at.

---

## Usage

**Dry run** (prints planned steps; no clone, push, or API writes):

```bash
python migrate_workflows.py \
  --dry-run \
  --repos owner/repo-a owner/repo-b
```

**Read repo list from file** (`#` line comments OK):

```bash
python migrate_workflows.py --repos-file repos.txt
```

Example `repos.txt`:

```text
# One slug or HTTPS URL per line
myorg/service-api
https://github.com/myorg/other-service
```

**Real run — fresh clones** (deletes `./migration-work/<owner>__<repo>/` first):

```bash
python migrate_workflows.py \
  --clean-workdir \
  --repos-file repos.txt \
  --env-file .env
```

**Reuse / overwrite remote migration branch:** If the feature branch already exists from a failed run:

```bash
python migrate_workflows.py --repos owner/repo-x --force-push
```

Uses **force-with-lease** derived from remote tip, falling back to plain `--force` if GitHub still reports stale refs (matches script behavior).

### Useful CLI flags

| Flag | Meaning |
|------|---------|
| `--branch` | Feature branch name (default: `DEVOPS-1450-move-workflow-to-central-devops`) |
| `--templates-dir` | Directory of template YAML files |
| `--codeowners-file` | SOURCE file for `.github/CODEOWNERS` |
| `--workdir-parent` | Where per-repo clones live (default `./migration-work`) |
| `--commit-message`, `--pr-title`, `--pr-body` | Repo-specific metadata |
| `--fail-fast` | Stop after first repo failure |
| `--env-file` | Dotenv path (default `./.env`) |

Defaults for commit message and PR text match the **`DEVOPS-1450`** ticket theme; override for your rollout.

Exit code **`0`** only if **all** repos succeed; **`1`** if any repo fails (**`2`** for bad arguments such as missing `--repos`).

---

## Limitations

- Targets must have **`main`** on `origin`; the script validates `refs/remotes/origin/main`.
- Quarantine fails if **`name-old.yml`** (or `.yaml`) **already exists** for a given workflow file.
- The script configures **local** `user.name` / `user.email` in each clone **only when unset**, using `workflow-migration@local` / `Workflow Migration` so **`git commit` succeeds** inside short-lived clones—adjust globally if org policy forbids placeholders.

---

## Security and hygiene

- Do **not** commit PATs or `repos.txt` with proprietary names if not appropriate for your remote.
- Clone/push uses an HTTPS URL containing the token; use a **dedicated** fine-scoped token and **revoke** after migration if policy requires.

---

## Dependencies

Listed in **`requirements.txt`**: `ruamel.yaml`, `python-dotenv`, `requests`.
