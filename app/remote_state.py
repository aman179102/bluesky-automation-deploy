from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

FILES = {
    "history": "history.json",
    "engagement": "engagement.json",
    "scheduler": "scheduler.json",
}


def _config() -> tuple[str, str, str, str]:
    return (
        os.getenv("STATE_BACKEND", "local").strip().lower(),
        os.getenv("STATE_GITHUB_TOKEN", "").strip(),
        os.getenv("STATE_GITHUB_REPO", "").strip(),
        os.getenv("STATE_GITHUB_BRANCH", "main").strip() or "main",
    )


def _backend() -> str:
    return _config()[0]


def _git_repo() -> str:
    return os.getenv("STATE_GIT_REPO", "").strip()


def _git_branch() -> str:
    return os.getenv("STATE_GIT_BRANCH", "main").strip() or "main"


def _git_worktree(settings: Settings) -> Path:
    configured = os.getenv("STATE_GIT_WORKTREE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / "bluesky-state-repo"


def _git_key(settings: Settings) -> Path | None:
    configured = os.getenv("STATE_GIT_SSH_KEY_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() else None
    encoded = os.getenv("STATE_GIT_SSH_KEY_B64", "").strip()
    raw = os.getenv("STATE_GIT_SSH_KEY", "")
    if encoded:
        try:
            raw = base64.b64decode(encoded).decode("utf-8")
        except Exception:
            logger.error("STATE_GIT_SSH_KEY_B64 is not valid base64")
            return None
    if not raw:
        return None
    path = Path(tempfile.gettempdir()) / "bluesky-state-key"
    path.write_text(raw if raw.endswith("\n") else raw + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _git_env(settings: Settings) -> dict[str, str] | None:
    key = _git_key(settings)
    if key is None:
        return None
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    return env


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s failed: %s", " ".join(args), exc)
        return False
    if completed.returncode != 0:
        logger.warning(
            "git %s exited %s: %s",
            " ".join(args),
            completed.returncode,
            (completed.stderr or completed.stdout).strip()[-1000:],
        )
        return False
    return True


def _ensure_clone(settings: Settings) -> bool:
    repo = _git_repo()
    worktree = _git_worktree(settings)
    env = _git_env(settings)
    if env is None:
        logger.error("No SSH key configured for git state")
        return False
    if (worktree / ".git").exists():
        if not _run_git(["fetch", "origin", _git_branch()], cwd=worktree, env=env):
            return False
        return _run_git(
            ["reset", "--hard", f"origin/{_git_branch()}"], cwd=worktree, env=env
        )
    worktree.parent.mkdir(parents=True, exist_ok=True)
    return _run_git(
        ["clone", "--depth", "1", "--branch", _git_branch(), repo, str(worktree)],
        env=env,
    )


def _git_pull(settings: Settings) -> bool:
    if not _ensure_clone(settings):
        return False
    path = _git_worktree(settings) / "state.json"
    if not path.exists():
        logger.info("No remote state yet; safe to initialize it")
        return True
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(bundle, dict):
            raise ValueError("remote state is not an object")
        _write_local(settings, bundle)
        logger.info("Pulled remote state from git (%s)", _git_repo())
        return True
    except Exception as exc:
        logger.warning("Could not read git remote state: %s", exc)
        return False


def _git_push(settings: Settings, message: str) -> bool:
    bundle = _read_local(settings)
    if not bundle:
        return False
    if not _ensure_clone(settings):
        return False
    worktree = _git_worktree(settings)
    path = worktree / "state.json"
    try:
        path.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("Could not write git state: %s", exc)
        return False
    env = _git_env(settings)
    if env is None:
        return False
    if not _run_git(["add", "state.json"], cwd=worktree, env=env):
        return False
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=worktree,
        env=env,
        capture_output=True,
    )
    if diff.returncode == 0:
        return True
    if not _run_git(
        [
            "-c",
            "user.name=Bluesky Automation",
            "-c",
            "user.email=bot@localhost",
            "commit",
            "-m",
            message,
        ],
        cwd=worktree,
        env=env,
    ):
        return False
    if not _run_git(
        ["push", "origin", f"HEAD:{_git_branch()}"], cwd=worktree, env=env
    ):
        if not _run_git(
            ["pull", "--rebase", "origin", _git_branch()], cwd=worktree, env=env
        ):
            return False
        if not _run_git(
            ["push", "origin", f"HEAD:{_git_branch()}"], cwd=worktree, env=env
        ):
            return False
    logger.info("Pushed remote state to git")
    return True


def remote_enabled() -> bool:
    backend, token, repo, _ = _config()
    if backend == "github":
        return bool(token and "/" in repo)
    if backend == "git":
        return bool(_git_repo() and _git_key(Settings()) is not None)
    return False


def validate_remote_config() -> tuple[bool, str]:
    backend, token, repo, _ = _config()
    if backend == "local":
        return True, "local state"
    if backend == "github":
        if not token:
            return False, "STATE_GITHUB_TOKEN is required when STATE_BACKEND=github"
        if not repo or "/" not in repo:
            return False, "STATE_GITHUB_REPO must be owner/repo"
        return True, "github state"
    if backend == "git":
        if not _git_repo():
            return False, "STATE_GIT_REPO is required when STATE_BACKEND=git"
        if _git_key(Settings()) is None:
            return False, "STATE_GIT_SSH_KEY_B64 or STATE_GIT_SSH_KEY_PATH is required"
        return True, "git state"
    return False, f"unknown STATE_BACKEND: {backend}"


def _api_url() -> str:
    _, _, repo, _ = _config()
    path = os.getenv("STATE_GITHUB_PATH", "state.json").strip().lstrip("/")
    return f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}"


def _headers() -> dict[str, str]:
    _, token, _, _ = _config()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _local_path(settings: Settings, name: str) -> Path:
    return settings.state_dir / name


def _read_local(settings: Settings) -> dict[str, Any]:
    bundle: dict[str, Any] = {}
    for key, name in FILES.items():
        path = _local_path(settings, name)
        if not path.exists():
            continue
        try:
            bundle[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Skipping unreadable local state file %s", path)
    return bundle


def _write_local(settings: Settings, bundle: dict[str, Any]) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    for key, name in FILES.items():
        payload = bundle.get(key)
        if not isinstance(payload, dict):
            continue
        path = _local_path(settings, name)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _github_pull(settings: Settings) -> bool:
    try:
        response = httpx.get(
            _api_url(),
            headers=_headers(),
            params={"ref": _config()[3]},
            timeout=20,
        )
        if response.status_code == 404:
            logger.info("No remote state yet; safe to initialize it")
            return True
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload.get("content") or "")
        bundle = json.loads(content.decode("utf-8"))
        if not isinstance(bundle, dict):
            raise ValueError("remote state is not an object")
        _write_local(settings, bundle)
        logger.info("Pulled remote state from GitHub (%s)", _config()[2])
        return True
    except Exception as exc:
        logger.warning("Could not pull remote state: %s", exc)
        return False


def _github_push(settings: Settings, message: str) -> bool:
    bundle = _read_local(settings)
    if not bundle:
        return False
    try:
        current = httpx.get(
            _api_url(),
            headers=_headers(),
            params={"ref": _config()[3]},
            timeout=20,
        )
        sha = ""
        if current.status_code == 200:
            sha = str(current.json().get("sha") or "")
        elif current.status_code != 404:
            current.raise_for_status()
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(
                json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
            ).decode("ascii"),
            "branch": _config()[3],
        }
        if sha:
            body["sha"] = sha
        response = httpx.put(
            _api_url(), headers=_headers(), json=body, timeout=30
        )
        if response.status_code == 409:
            latest = httpx.get(
                _api_url(),
                headers=_headers(),
                params={"ref": _config()[3]},
                timeout=20,
            )
            if latest.status_code == 200:
                body["sha"] = str(latest.json().get("sha") or "")
            response = httpx.put(
                _api_url(), headers=_headers(), json=body, timeout=30
            )
        response.raise_for_status()
        logger.info("Pushed remote state to GitHub")
        return True
    except Exception as exc:
        logger.warning("Could not push remote state: %s", exc)
        return False


def pull_state(settings: Settings) -> bool:
    if not remote_enabled():
        return False
    if _backend() == "git":
        return _git_pull(settings)
    return _github_pull(settings)


def push_state(settings: Settings, message: str = "chore: update bot state") -> bool:
    if not remote_enabled():
        return False
    if _backend() == "git":
        return _git_push(settings, message)
    return _github_push(settings, message)


def main() -> int:
    import argparse

    from app.config import load_settings

    parser = argparse.ArgumentParser(description="Inspect or sync bot state remotely.")
    parser.add_argument("action", choices=["pull", "push"])
    parser.add_argument("--message", default="chore: update bot state")
    args = parser.parse_args()
    settings = load_settings()
    ok, message = validate_remote_config()
    if not ok:
        print(f"Invalid remote state configuration: {message}")
        return 1
    if not remote_enabled():
        print("Remote state is disabled; check STATE_BACKEND and credentials")
        return 1
    changed = pull_state(settings) if args.action == "pull" else push_state(settings, args.message)
    print(f"{args.action}: {'ok' if changed else 'failed'}")
    return 0 if changed else 1


if __name__ == "__main__":
    raise SystemExit(main())
