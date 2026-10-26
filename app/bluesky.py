from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.models import SourceRef

logger = logging.getLogger(__name__)

TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"
TID_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class BlueskyError(RuntimeError):
    pass


def make_tid(value: datetime, clock_id: int = 0) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - TID_EPOCH
    timestamp_us = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )
    combined = ((timestamp_us & ((1 << 53) - 1)) << 10) | (clock_id & 0x3FF)
    chars: list[str] = []
    for _ in range(13):
        chars.append(TID_ALPHABET[combined & 0x1F])
        combined >>= 5
    return "".join(reversed(chars))


def profile_has_bot_label(record: dict[str, Any]) -> bool:
    labels = record.get("labels") or {}
    if labels.get("$type") != "com.atproto.label.defs#selfLabels":
        return False
    values = labels.get("values") or []
    return any(value.get("val") == "bot" for value in values if isinstance(value, dict))


def utc_timestamp(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def build_facets(text: str, refs: list[SourceRef]) -> list[dict[str, Any]]:
    if not text or not refs:
        return []
    lower = text.lower()
    source_start = lower.rfind("source")
    if source_start < 0:
        source_start = 0
    facets: list[dict[str, Any]] = []
    used: list[tuple[int, int]] = []

    def add_span(needle: str, url: str) -> None:
        if not needle:
            return
        index = text.lower().find(needle.lower(), source_start)
        if index < 0:
            return
        end = index + len(needle)
        if any(not (end <= start or index >= finish) for start, finish in used):
            return
        byte_start = len(text[:index].encode("utf-8"))
        byte_end = len(text[:end].encode("utf-8"))
        used.append((index, end))
        facets.append(
            {
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": url,
                    }
                ],
            }
        )

    for ref in refs[:2]:
        add_span(ref.name, ref.url)
        short = ref.short
        if short and short.lower() not in ref.name.lower():
            add_span(short, ref.url)

    facets.sort(key=lambda item: item["index"]["byteStart"])
    return facets


class BlueskyClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.did = ""
        self.handle = settings.handle
        self.access_jwt = ""
        self.client = httpx.Client(
            base_url=settings.pds_url,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "BlueskyClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _auth_headers(self) -> dict[str, str]:
        if not self.access_jwt:
            raise BlueskyError("Not logged in")
        return {"Authorization": f"Bearer {self.access_jwt}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
        expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if auth:
            request_headers.update(self._auth_headers())
        response = self.client.request(
            method,
            path,
            params=params,
            json=json,
            content=content,
            headers=request_headers,
        )
        if response.status_code not in expected:
            detail = response.text[:500].replace("\n", " ")
            raise BlueskyError(
                f"Bluesky {method} {path} returned HTTP {response.status_code}: {detail}"
            )
        return response

    def login(self) -> None:
        if not self.settings.handle or not self.settings.app_password:
            raise BlueskyError("BSKY_HANDLE and BSKY_APP_PASSWORD are required")
        response = self._request(
            "POST",
            "/xrpc/com.atproto.server.createSession",
            json={
                "identifier": self.settings.handle,
                "password": self.settings.app_password,
            },
            auth=False,
        )
        payload = response.json()
        self.access_jwt = payload["accessJwt"]
        self.did = payload["did"]
        self.handle = payload.get("handle") or self.handle
        logger.info("Logged in as %s (%s)", self.handle, self.did)

    def get_profile_record(self) -> dict[str, Any] | None:
        try:
            response = self._request(
                "GET",
                "/xrpc/com.atproto.repo.getRecord",
                params={
                    "repo": self.did,
                    "collection": "app.bsky.actor.profile",
                    "rkey": "self",
                },
            )
        except BlueskyError as exc:
            if "RecordNotFound" in str(exc) or "HTTP 400" in str(exc):
                return None
            raise
        return response.json().get("value")

    def has_bot_label(self) -> bool:
        return profile_has_bot_label(self.get_profile_record() or {})

    def set_bot_label(self) -> dict[str, Any]:
        record = self.get_profile_record() or {
            "$type": "app.bsky.actor.profile",
        }
        labels = record.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        values = labels.get("values")
        if not isinstance(values, list):
            values = []
        if not any(value.get("val") == "bot" for value in values if isinstance(value, dict)):
            values.append({"val": "bot"})
        labels = {
            "$type": "com.atproto.label.defs#selfLabels",
            "values": values,
        }
        record["labels"] = labels
        response = self._request(
            "POST",
            "/xrpc/com.atproto.repo.putRecord",
            json={
                "repo": self.did,
                "collection": "app.bsky.actor.profile",
                "rkey": "self",
                "record": record,
            },
        )
        return response.json()

    def remove_bot_label(self) -> dict[str, Any]:
        record = self.get_profile_record()
        if not record:
            return {"status": "profile_missing"}
        labels = record.get("labels")
        if not isinstance(labels, dict):
            return {"status": "bot_label_absent"}
        values = labels.get("values")
        if not isinstance(values, list):
            return {"status": "bot_label_absent"}
        remaining = [
            value
            for value in values
            if not (isinstance(value, dict) and value.get("val") == "bot")
        ]
        if len(remaining) == len(values):
            return {"status": "bot_label_absent"}
        if remaining:
            labels["values"] = remaining
            record["labels"] = labels
        else:
            record.pop("labels", None)
        response = self._request(
            "POST",
            "/xrpc/com.atproto.repo.putRecord",
            json={
                "repo": self.did,
                "collection": "app.bsky.actor.profile",
                "rkey": "self",
                "record": record,
            },
        )
        return response.json()

    def get_profile(self, actor: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/xrpc/app.bsky.actor.getProfile",
            params={"actor": actor},
        )
        return response.json()

    def get_author_feed(
        self,
        actor: str,
        *,
        limit: int = 50,
        filter: str = "posts_no_replies",
    ) -> dict[str, Any]:
        response = self._request(
            "GET",
            "/xrpc/app.bsky.feed.getAuthorFeed",
            params={"actor": actor, "limit": min(max(limit, 1), 100), "filter": filter},
        )
        return response.json()

    def search_posts(
        self,
        query: str,
        *,
        limit: int = 25,
        sort: str = "top",
    ) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            "/xrpc/app.bsky.feed.searchPosts",
            params={"q": query, "limit": min(max(limit, 1), 100), "sort": sort},
        )
        return list(response.json().get("posts") or [])

    def create_reply(
        self,
        *,
        text: str,
        parent_uri: str,
        parent_cid: str,
        root_uri: str | None = None,
        root_cid: str | None = None,
        rkey: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        created_at = created_at or datetime.now(timezone.utc)
        rkey = rkey or make_tid(created_at)
        existing = self.get_post_record(rkey)
        if existing:
            logger.info("Reply already exists for rkey %s; skipping", rkey)
            return existing

        root = {"uri": root_uri or parent_uri, "cid": root_cid or parent_cid}
        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": utc_timestamp(created_at),
            "langs": ["en"],
            "reply": {
                "root": root,
                "parent": {"uri": parent_uri, "cid": parent_cid},
            },
        }
        try:
            response = self._request(
                "POST",
                "/xrpc/com.atproto.repo.createRecord",
                json={
                    "repo": self.did,
                    "collection": "app.bsky.feed.post",
                    "rkey": rkey,
                    "record": record,
                },
            )
        except BlueskyError as exc:
            if "already exists" in str(exc).lower():
                existing = self.get_post_record(rkey)
                if existing:
                    return existing
            raise
        payload = response.json()
        logger.info("Published reply %s", payload.get("uri"))
        return payload

    def upload_image(self, path_or_bytes: Any) -> dict[str, Any]:
        if isinstance(path_or_bytes, (str, bytes)):
            if isinstance(path_or_bytes, str):
                content = open(path_or_bytes, "rb").read()
            else:
                content = path_or_bytes
        else:
            content = path_or_bytes.read_bytes()
        response = self._request(
            "POST",
            "/xrpc/com.atproto.repo.uploadBlob",
            content=content,
            headers={"Content-Type": "image/jpeg"},
        )
        return response.json()["blob"]

    def get_post_record(self, rkey: str) -> dict[str, Any] | None:
        try:
            response = self._request(
                "GET",
                "/xrpc/com.atproto.repo.getRecord",
                params={
                    "repo": self.did,
                    "collection": "app.bsky.feed.post",
                    "rkey": rkey,
                },
            )
        except BlueskyError as exc:
            if "RecordNotFound" in str(exc) or "HTTP 400" in str(exc):
                return None
            raise
        return response.json()

    def create_post(
        self,
        *,
        text: str,
        facets: list[dict[str, Any]],
        image_path: Any | None = None,
        alt_text: str = "",
        rkey: str = "",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        existing = self.get_post_record(rkey)
        if existing:
            logger.info("Post already exists for rkey %s; skipping", rkey)
            return existing

        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": utc_timestamp(created_at),
            "langs": ["en"],
        }
        if image_path is not None:
            blob = self.upload_image(image_path)
            record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": [
                    {
                        "alt": alt_text,
                        "image": blob,
                    }
                ],
            }
        if facets:
            record["facets"] = facets
        try:
            response = self._request(
                "POST",
                "/xrpc/com.atproto.repo.createRecord",
                json={
                    "repo": self.did,
                    "collection": "app.bsky.feed.post",
                    "rkey": rkey,
                    "record": record,
                },
            )
        except BlueskyError as exc:
            if "already exists" in str(exc).lower():
                existing = self.get_post_record(rkey)
                if existing:
                    return existing
            raise
        payload = response.json()
        logger.info("Published post %s", payload.get("uri"))
        return payload

    def delete_post(self, rkey: str) -> None:
        self._request(
            "POST",
            "/xrpc/com.atproto.repo.deleteRecord",
            json={
                "repo": self.did,
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
            },
        )
