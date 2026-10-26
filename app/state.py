from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models import StoryItem
from app.text import canonical_url, title_fingerprint

DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "posts": [],
    "seen": [],
    "last_run": None,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(DEFAULT_STATE))
    if not isinstance(payload, dict):
        return json.loads(json.dumps(DEFAULT_STATE))
    for key, value in DEFAULT_STATE.items():
        payload.setdefault(key, json.loads(json.dumps(value)))
    if not isinstance(payload.get("posts"), list):
        payload["posts"] = []
    if not isinstance(payload.get("seen"), list):
        payload["seen"] = []
    return payload


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=".history-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as handle_file:
            json.dump(state, handle_file, ensure_ascii=False, indent=2)
            handle_file.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def prune_state(state: dict[str, Any], now: datetime, days: int) -> None:
    cutoff = now - timedelta(days=days)
    seen = []
    for entry in state.get("seen", []):
        try:
            seen_at = datetime.fromisoformat(entry.get("seen_at", ""))
        except (TypeError, ValueError):
            continue
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        if seen_at >= cutoff:
            seen.append(entry)
    state["seen"] = seen

    posts = []
    for entry in state.get("posts", []):
        try:
            posted_at = datetime.fromisoformat(entry.get("posted_at", ""))
        except (TypeError, ValueError):
            continue
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
        if posted_at >= cutoff:
            posts.append(entry)
    state["posts"] = posts


def seen_keys(state: dict[str, Any]) -> tuple[set[str], set[str]]:
    urls: set[str] = set()
    fingerprints: set[str] = set()
    for entry in state.get("seen", []):
        url = canonical_url(entry.get("url", ""))
        if url:
            urls.add(url)
        fingerprint = entry.get("fingerprint") or title_fingerprint(entry.get("title", ""))
        if fingerprint:
            fingerprints.add(fingerprint)
    return urls, fingerprints


def was_seen(state: dict[str, Any], item: StoryItem) -> bool:
    urls, fingerprints = seen_keys(state)
    if canonical_url(item.url) in urls:
        return True
    return title_fingerprint(item.title) in fingerprints


def mark_seen(state: dict[str, Any], item: StoryItem, now: datetime) -> None:
    state.setdefault("seen", []).append(
        {
            "url": canonical_url(item.url),
            "title": item.title,
            "fingerprint": title_fingerprint(item.title),
            "source": item.source,
            "seen_at": now.isoformat(),
        }
    )


def record_post(
    state: dict[str, Any],
    *,
    uri: str,
    cid: str,
    rkey: str,
    text: str,
    title: str,
    url: str,
    category: str,
    source_names: list[str],
    posted_at: datetime,
    breaking: bool = False,
) -> None:
    state.setdefault("posts", []).append(
        {
            "uri": uri,
            "cid": cid,
            "rkey": rkey,
            "text": text,
            "title": title,
            "url": canonical_url(url),
            "category": category,
            "source_names": source_names,
            "posted_at": posted_at.isoformat(),
            "breaking": breaking,
        }
    )
    state["last_run"] = posted_at.isoformat()


def recent_categories(state: dict[str, Any], limit: int = 10) -> list[str]:
    posts = sorted(
        state.get("posts", []),
        key=lambda item: item.get("posted_at", ""),
        reverse=True,
    )
    return [str(item.get("category", "")) for item in posts[:limit] if item.get("category")]


def recent_sources(state: dict[str, Any], limit: int = 10) -> list[str]:
    posts = sorted(
        state.get("posts", []),
        key=lambda item: item.get("posted_at", ""),
        reverse=True,
    )
    values: list[str] = []
    for item in posts[:limit]:
        values.extend(str(name) for name in item.get("source_names", []))
    return values
