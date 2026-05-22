#!/usr/bin/env python3
"""
Clone target repo(s) from main, quarantine existing workflows, copy central templates
and CODEOWNERS, push a branch, and open a PR into main.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from io import StringIO
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from ruamel.yaml import YAML

BASE_BRANCH = "main"
OLD_PREFIX = "Old "

# Workflow YAML templates copied into each target repo's .github/workflows/
# (maintain filenames here). Use --templates-dir to point at a folder with extras.
TEMPLATE_FILES = ("workflow.yml",)


def tool_root() -> Path:
    return Path(__file__).resolve().parent


def parse_repo_slug(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty repo slug")
    if raw.startswith("https://") or raw.startswith("http://"):
        parsed = urlparse(raw)
        path = parsed.path.strip("/").removesuffix(".git")
        parts = path.split("/")
        if len(parts) < 2:
            raise ValueError(f"could not parse owner/repo from URL: {raw!r}")
        return parts[-2], parts[-1]
    if "/" not in raw:
        raise ValueError(f"expected owner/repo, got: {raw!r}")
    owner, repo = raw.split("/", 1)
    repo = repo.removesuffix(".git")
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        raise ValueError(f"invalid owner/repo: {raw!r}")
    return owner, repo


def parse_remote_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Return (owner, repo) if remote looks like github.com owner/repo."""
    u = remote_url.strip()
    if not u:
        return None
    if u.startswith("git@"):
        _, sep, rest = u.partition(":")
        if not sep:
            return None
        rest = rest.strip().removesuffix(".git")
        if "github.com" not in u.lower():
            return None
        parts = rest.split("/")
        if len(parts) == 2:
            return parts[0], parts[1]
        return None
    if "://" in u:
        scheme, rest = u.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        u = f"{scheme}://{rest}"
    parsed = urlparse(u)
    host = (parsed.netloc or "").lower()
    if "github.com" not in host:
        return None
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def read_repos_file(path: Path) -> list[str]:
    out: list[str] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def collect_repo_slugs(args: argparse.Namespace) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for r in args.repos or []:
        key = r.strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    if args.repos_file:
        for line in read_repos_file(Path(args.repos_file)):
            key = line.strip()
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
    if not ordered:
        raise SystemExit("No repositories specified. Use --repos and/or --repos-file.")
    return ordered


def resolve_github_token(env_file: Path | None, dry_run: bool) -> str:
    if env_file and env_file.is_file():
        load_dotenv(env_file, override=True)
    else:
        load_dotenv(override=True)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token and not dry_run:
        raise SystemExit(
            "Missing GITHUB_TOKEN or GH_TOKEN. Set one in .env or the environment."
        )
    return token or ""


def authed_remote_url(owner: str, repo: str, token: str) -> str:
    return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"


def sanitize_workdir_name(owner: str, repo: str) -> str:
    safe_owner = re.sub(r"[^A-Za-z0-9._-]+", "_", owner)
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "_", repo)
    return f"{safe_owner}__{safe_repo}"


def run_git(
    repo_dir: Path, *git_args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run git; default check=False so failures return stdout/stderr for git_require_ok."""
    return subprocess.run(
        ("git",) + git_args,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=check,
    )


def git_require_ok(proc: subprocess.CompletedProcess[str], what: str) -> None:
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        out = (proc.stdout or "").strip()
        combined = "\n".join(p for p in (err, out) if p)
        if not combined:
            combined = f"exit code {proc.returncode}"
        raise RuntimeError(f"{what} failed:\n{combined}")


def verify_origin_has_main(repo_dir: Path) -> None:
    proc = run_git(
        repo_dir,
        "rev-parse",
        "--verify",
        f"refs/remotes/origin/{BASE_BRANCH}",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Branch {BASE_BRANCH!r} not found on origin "
            f"(required: clone must have {BASE_BRANCH})."
        )


def _yaml_rt() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    return y


def prefix_workflow_top_level_name(content: str) -> str:
    y = _yaml_rt()
    try:
        data = y.load(StringIO(content))
    except Exception as exc:
        raise RuntimeError(f"invalid workflow YAML: {exc}") from exc
    if not isinstance(data, dict):
        return content
    name = data.get("name")
    if name is not None and not (
        isinstance(name, str) and name.startswith(OLD_PREFIX)
    ):
        text = str(name).strip()
        if text:
            data["name"] = OLD_PREFIX + text
    out = StringIO()
    y.dump(data, out)
    return out.getvalue()


def quarantine_workflows(workflows_dir: Path, dry_run: bool) -> None:
    if not workflows_dir.is_dir():
        return
    candidates = sorted(
        p
        for p in workflows_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".yml", ".yaml")
    )
    for path in candidates:
        new_name = f"{path.stem}-old{path.suffix}"
        dest = path.parent / new_name
        if dest.exists():
            raise RuntimeError(
                f"Cannot rename {path.name!r} to {new_name!r}: destination exists."
            )
        if dry_run:
            print(f"  [dry-run] quarantine: {path.name} -> {new_name}")
            _dry_run_name_preview(path)
            continue
        original = path.read_text(encoding="utf-8")
        updated = prefix_workflow_top_level_name(original)
        dest.write_text(updated, encoding="utf-8")
        path.unlink()


def _dry_run_name_preview(path: Path) -> None:
    try:
        data = _yaml_rt().load(StringIO(path.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"  [dry-run] (skip name preview: {exc})")
        return
    if isinstance(data, dict) and "name" in data:
        n = data["name"]
        if n is not None:
            s = str(n).strip()
            if s and not s.startswith(OLD_PREFIX):
                print(f"  [dry-run]   workflow name: {s!r} -> {OLD_PREFIX + s!r}")


def copy_templates(templates_dir: Path, dest_workflows: Path, dry_run: bool) -> None:
    missing = [n for n in TEMPLATE_FILES if not (templates_dir / n).is_file()]
    if missing:
        raise RuntimeError(f"Missing template(s) in {templates_dir}: {', '.join(missing)}")
    if dry_run:
        for name in TEMPLATE_FILES:
            print(f"  [dry-run] copy template: {templates_dir / name} -> {dest_workflows / name}")
        return
    dest_workflows.mkdir(parents=True, exist_ok=True)
    for name in TEMPLATE_FILES:
        shutil.copy2(templates_dir / name, dest_workflows / name)


def copy_codeowners(codeowners_file: Path, dest_github: Path, dry_run: bool) -> None:
    if not codeowners_file.is_file():
        raise RuntimeError(f"CODEOWNERS source not found: {codeowners_file}")
    dest = dest_github / "CODEOWNERS"
    if dry_run:
        print(f"  [dry-run] copy: {codeowners_file} -> {dest}")
        return
    dest_github.mkdir(parents=True, exist_ok=True)
    shutil.copy2(codeowners_file, dest)


def git_configure_committer(repo_dir: Path) -> None:
    proc = run_git(repo_dir, "config", "--get", "user.email", check=False)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        git_require_ok(
            run_git(
                repo_dir, "config", "user.email", "workflow-migration@local"
            ),
            "git config user.email",
        )
    proc = run_git(repo_dir, "config", "--get", "user.name", check=False)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        git_require_ok(
            run_git(repo_dir, "config", "user.name", "Workflow Migration"),
            "git config user.name",
        )


def prepare_local_repo(
    owner: str,
    repo: str,
    token: str,
    dest: Path,
    dry_run: bool,
    clean_workdir: bool,
) -> None:
    """
    Ensure a local clone at dest: clone into a new directory, or reuse an existing
    clone when it matches owner/repo (fetch + reset to origin/main + clean).
    With clean_workdir, delete dest first when it exists.
    """
    url = authed_remote_url(owner, repo, token)
    if dry_run:
        extra = "; --clean-workdir" if clean_workdir else ""
        print(
            f"  [dry-run] prepare repo at {dest} (clone or reuse if same repo{extra})"
        )
        return
    if clean_workdir and dest.exists():
        shutil.rmtree(dest)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                "git",
                "clone",
                "-b",
                BASE_BRANCH,
                "--single-branch",
                url,
                str(dest),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"git clone failed for {owner}/{repo}: {err}")
        return

    if not (dest / ".git").exists():
        raise RuntimeError(
            f"{dest} exists and is not a git repository. "
            "Remove it or pass --clean-workdir to delete and re-clone."
        )
    proc = run_git(dest, "remote", "get-url", "origin", check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{dest} has no origin remote. Remove it or use --clean-workdir."
        )
    remote = (proc.stdout or "").strip()
    parsed = parse_remote_owner_repo(remote)
    same = (
        parsed is not None
        and parsed[0].lower() == owner.lower()
        and parsed[1].lower() == repo.lower()
    )
    if not same:
        raise RuntimeError(
            f"{dest} is a clone of {parsed or remote!r}, expected {owner}/{repo}. "
            "Remove it, use another --workdir-parent, or pass --clean-workdir."
        )

    print(
        f"  Reusing existing clone at {dest} "
        f"(fetch, reset to origin/{BASE_BRANCH}, clean untracked)"
    )
    set_origin_token(dest, owner, repo, token)
    git_require_ok(run_git(dest, "fetch", "origin"), "git fetch")
    git_require_ok(run_git(dest, "checkout", BASE_BRANCH), f"git checkout {BASE_BRANCH}")
    git_require_ok(
        run_git(dest, "reset", "--hard", f"origin/{BASE_BRANCH}"),
        f"git reset --hard origin/{BASE_BRANCH}",
    )
    git_require_ok(run_git(dest, "clean", "-fd"), "git clean -fd")


def set_origin_token(repo_dir: Path, owner: str, repo: str, token: str) -> None:
    if not repo_dir.is_dir():
        return
    url = authed_remote_url(owner, repo, token)
    git_require_ok(
        run_git(repo_dir, "remote", "set-url", "origin", url),
        "git remote set-url origin",
    )


def remote_feature_branch_tip_sha(repo_dir: Path, feature_branch: str) -> str | None:
    """Return current refs/heads/<branch> commit on origin, or None if absent."""
    proc = run_git(repo_dir, "ls-remote", "--heads", "origin", feature_branch)
    git_require_ok(proc, "git ls-remote origin")
    want = f"refs/heads/{feature_branch}"
    for line in (proc.stdout or "").strip().splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == want:
            return parts[0]
    return None


def commit_and_push(
    repo_dir: Path,
    owner: str,
    repo: str,
    token: str,
    feature_branch: str,
    message: str,
    dry_run: bool,
    force_with_lease: bool,
) -> None:
    if dry_run:
        extra = " --force-with-lease" if force_with_lease else ""
        print(
            f"  [dry-run] git add / commit / push{extra} origin {feature_branch}"
        )
        return
    git_configure_committer(repo_dir)
    git_require_ok(run_git(repo_dir, "add", "-A"), "git add")
    st = run_git(repo_dir, "status", "--porcelain")
    if not (st.stdout or "").strip():
        raise RuntimeError("No changes to commit after migration steps.")
    git_require_ok(run_git(repo_dir, "commit", "-m", message), "git commit")
    set_origin_token(repo_dir, owner, repo, token)
    if force_with_lease:
        remote_tip = remote_feature_branch_tip_sha(repo_dir, feature_branch)
        if remote_tip:
            proc = run_git(
                repo_dir,
                "push",
                "-u",
                f"--force-with-lease=refs/heads/{feature_branch}:{remote_tip}",
                "origin",
                feature_branch,
            )
            err_text = ((proc.stderr or "") + (proc.stdout or "")).lower()
            if proc.returncode != 0 and "stale info" in err_text:
                proc = run_git(
                    repo_dir,
                    "push",
                    "-u",
                    "--force",
                    "origin",
                    feature_branch,
                )
        else:
            proc = run_git(repo_dir, "push", "-u", "origin", feature_branch)
    else:
        proc = run_git(repo_dir, "push", "-u", "origin", feature_branch)
    if proc.returncode != 0 and not force_with_lease:
        err = ((proc.stderr or "") + (proc.stdout or "")).lower()
        if any(
            x in err
            for x in (
                "non-fast-forward",
                "reject",
                "failed to push",
                "updates were rejected",
            )
        ):
            raise RuntimeError(
                "git push failed (remote branch may already exist from a prior run).\n"
                f"{(proc.stderr or proc.stdout or '').strip()}\n"
                f"Retry with --force-push to update remote branch {feature_branch!r}."
            )
    git_require_ok(proc, "git push")


def _github_api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _format_github_validation_body(data: dict, fallback: str) -> str:
    lines: list[str] = []
    m = data.get("message")
    if m:
        lines.append(str(m))
    for err in data.get("errors") or []:
        if isinstance(err, dict):
            piece = err.get("message") or err.get("code") or ""
            if piece:
                lines.append(str(piece))
        elif err:
            lines.append(str(err))
    return "\n".join(lines) if lines else fallback


def find_open_pull_for_head(
    owner: str, repo: str, token: str, head_branch: str
) -> str | None:
    """Open PR HTML URL where head is owner:branch and base is main, if one exists."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    # head filter: user_or_org:branch (required for disambiguation)
    resp = requests.get(
        url,
        headers=_github_api_headers(token),
        params={
            "state": "open",
            "head": f"{owner}:{head_branch}",
            "base": BASE_BRANCH,
            "per_page": 10,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        return None
    for pr in resp.json():
        if (
            isinstance(pr, dict)
            and pr.get("base", {}).get("ref") == BASE_BRANCH
            and pr.get("head", {}).get("ref") == head_branch
        ):
            html = pr.get("html_url")
            if html:
                return str(html)
    return None


def _pr_error_implies_duplicate(data: dict) -> bool:
    blob = json.dumps(data).lower()
    if "already exists" in blob:
        return True
    for err in data.get("errors") or []:
        if isinstance(err, dict) and "already exists" in str(err.get("message", "")).lower():
            return True
    return False


def create_pull_request(
    owner: str,
    repo: str,
    token: str,
    head_branch: str,
    title: str,
    body: str,
    dry_run: bool,
) -> str | None:
    if dry_run:
        print(f"  [dry-run] POST /repos/{owner}/{repo}/pulls base={BASE_BRANCH} head={head_branch}")
        return None
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = _github_api_headers(token)
    payload = {
        "title": title,
        "body": body,
        "head": head_branch,
        "base": BASE_BRANCH,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    if resp.status_code == 422:
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(
                f"GitHub rejected PR (422): {resp.text.strip() or 'no JSON body'}"
            ) from None
        if _pr_error_implies_duplicate(data):
            existing = find_open_pull_for_head(owner, repo, token, head_branch)
            if existing:
                return existing
        detail = _format_github_validation_body(data, resp.text)
        raise RuntimeError(f"GitHub rejected PR (422):\n{detail}")
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"GitHub PR create failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    return str(data.get("html_url", ""))


def migrate_one_repo(
    slug: str,
    token: str,
    workdir_parent: Path,
    templates_dir: Path,
    codeowners_file: Path,
    feature_branch: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    dry_run: bool,
    clean_workdir: bool,
    force_push: bool,
) -> str | None:
    owner, repo = parse_repo_slug(slug)
    print(f"\n=== {owner}/{repo} ===")
    clone_dest = workdir_parent / sanitize_workdir_name(owner, repo)
    workflows = clone_dest / ".github" / "workflows"
    github_dir = clone_dest / ".github"

    prepare_local_repo(owner, repo, token, clone_dest, dry_run, clean_workdir)

    if dry_run:
        print(f"  [dry-run] migration steps would run in {clone_dest}")
        quarantine_workflows(workflows, dry_run=True)
        copy_templates(templates_dir, workflows, dry_run=True)
        copy_codeowners(codeowners_file, github_dir, dry_run=True)
        commit_and_push(
            clone_dest,
            owner,
            repo,
            token,
            feature_branch,
            commit_message,
            dry_run=True,
            force_with_lease=force_push,
        )
        create_pull_request(
            owner, repo, token, feature_branch, pr_title, pr_body, dry_run=True
        )
        return None

    repo_dir = clone_dest
    if not repo_dir.is_dir():
        raise RuntimeError(f"Expected cloned repo at {repo_dir}")

    verify_origin_has_main(repo_dir)
    proc = run_git(repo_dir, "checkout", BASE_BRANCH)
    git_require_ok(proc, f"git checkout {BASE_BRANCH}")
    proc = run_git(repo_dir, "checkout", "-b", feature_branch, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").lower()
        if "already exists" in err:
            proc2 = run_git(repo_dir, "checkout", feature_branch)
            git_require_ok(proc2, "git checkout existing feature branch")
            proc3 = run_git(repo_dir, "reset", "--hard", f"origin/{BASE_BRANCH}")
            git_require_ok(proc3, f"git reset --hard origin/{BASE_BRANCH}")
        else:
            git_require_ok(proc, "git checkout -b")

    if workflows.is_dir():
        quarantine_workflows(workflows, dry_run=False)
    copy_templates(templates_dir, workflows, dry_run=False)
    copy_codeowners(codeowners_file, github_dir, dry_run=False)
    commit_and_push(
        repo_dir,
        owner,
        repo,
        token,
        feature_branch,
        commit_message,
        dry_run=False,
        force_with_lease=force_push,
    )
    pr_url = create_pull_request(
        owner, repo, token, feature_branch, pr_title, pr_body, dry_run=False
    )
    if pr_url:
        print(f"  PR: {pr_url}")
    return pr_url


def build_arg_parser() -> argparse.ArgumentParser:
    default_branch = "DEVOPS-1450-move-workflow-to-central-devops"
    tr = tool_root()
    p = argparse.ArgumentParser(
        description="Migrate workflows: quarantine old files, add central templates, open PR to main."
    )
    p.add_argument(
        "--repos",
        nargs="*",
        default=[],
        metavar="OWNER/REPO",
        help="One or more owner/repo slugs or https://github.com/... URLs",
    )
    p.add_argument(
        "--repos-file",
        metavar="PATH",
        help="File with one repo per line (# comments allowed)",
    )
    p.add_argument(
        "--branch",
        default=default_branch,
        help=f"Feature branch to create from {BASE_BRANCH} (default: %(default)s)",
    )
    p.add_argument(
        "--templates-dir",
        type=Path,
        default=tr / "workflows",
        help="Directory containing template workflow files (default: <tool>/workflows)",
    )
    p.add_argument(
        "--codeowners-file",
        type=Path,
        default=tr / "codeowners" / "CODEOWNERS",
        help="Path to CODEOWNERS template (default: <tool>/codeowners/CODEOWNERS)",
    )
    p.add_argument(
        "--workdir-parent",
        type=Path,
        default=tr / "migration-work",
        help="Parent directory for per-repo clone folders (default: <tool>/migration-work)",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Dotenv file to load (default: ./.env)",
    )
    p.add_argument(
        "--commit-message",
        default="DEVOPS-1450: migrate workflows to central devops",
        help="Git commit message",
    )
    p.add_argument(
        "--pr-title",
        default="DEVOPS-1450: Move workflows to central devops",
        help="Pull request title",
    )
    p.add_argument(
        "--pr-body",
        default=(
            "Migrates GitHub Actions to shared templates from the devops repo.\n\n"
            "- Existing workflows renamed with `-old` suffix and prefixed with `Old ` in `name`.\n"
            "- Central workflow templates and `.github/CODEOWNERS` added."
        ),
        help="Pull request body",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without cloning, pushing, or calling the API",
    )
    p.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first repo error (default: print the error and continue)",
    )
    p.add_argument(
        "--clean-workdir",
        action="store_true",
        help=(
            "Delete the per-repo folder under --workdir-parent before cloning "
            "(forces a fresh clone)"
        ),
    )
    p.add_argument(
        "--force-push",
        action="store_true",
        help=(
            "Overwrite the remote feature branch when it already exists: use an "
            "explicit force-with-lease from git ls-remote, then plain --force if "
            "the server still reports stale info (safe for re-running this migration)"
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    env_path = args.env_file
    if not env_path.is_file() and not args.dry_run:
        print(f"Warning: env file not found: {env_path} (using process environment only)", file=sys.stderr)

    token = resolve_github_token(
        env_path if env_path.is_file() else None, dry_run=args.dry_run
    )

    try:
        slugs = collect_repo_slugs(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 2

    workdir_parent: Path = args.workdir_parent
    feature_branch: str = args.branch
    failures: list[str] = []
    successes: list[str] = []

    for slug in slugs:
        try:
            migrate_one_repo(
                slug=slug,
                token=token,
                workdir_parent=workdir_parent,
                templates_dir=args.templates_dir.resolve(),
                codeowners_file=args.codeowners_file.resolve(),
                feature_branch=feature_branch,
                commit_message=args.commit_message,
                pr_title=args.pr_title,
                pr_body=args.pr_body,
                dry_run=args.dry_run,
                clean_workdir=args.clean_workdir,
                force_push=args.force_push,
            )
            successes.append(slug)
        except Exception as exc:
            print(f"ERROR {slug}: {exc}", file=sys.stderr)
            failures.append(slug)
            if args.fail_fast:
                print("\nStopped early (--fail-fast).", file=sys.stderr)
                break

    if args.dry_run:
        print("\nDry run finished (no changes made).")
    else:
        processed = len(successes) + len(failures)
        print(
            f"\nDone. Success: {len(successes)}, failed: {len(failures)}, "
            f"repos in list: {len(slugs)}."
        )
        if args.fail_fast and failures and processed < len(slugs):
            print(
                "Some repos were skipped (--fail-fast stopped after first error).",
                file=sys.stderr,
            )
        if failures:
            print("Failed repos:", file=sys.stderr)
            for s in failures:
                print(f"  - {s}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
