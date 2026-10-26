from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.ai import generate_json
from app.bluesky import BlueskyClient
from app.compliance import FIRST_PERSON_RE, is_disallowed_text
from app.config import Settings
from app.text import normalize_space

logger = logging.getLogger(__name__)

DEFAULT_ENGAGEMENT_STATE: dict[str, Any] = {
    "version": 1,
    "replies": [],
    "last_run": None,
}

DEFAULT_GROWTH_STATE: dict[str, Any] = {
    "version": 1,
    "snapshots": [],
}

ENGAGEMENT_BLOCKED_RE = re.compile(
    r"\b(?:plutocracy|attorney general|campaign|political|politics|partisan|"
    r"musk|paxton|trump|biden|harris|election|senate|congress|government shutdown|"
    r"politician|politicians)\b",
    re.IGNORECASE,
)

ENGAGEMENT_OPINION_RE = re.compile(
    r"\b(?:gonna|honestly|honest|imo|imho|tbh|for a sec|these days|"
    r"hot take|unpopular opinion|please stop|minimi[sz]e|illegally|"
    r"corrupt|evil|shame on|boycott|derogatory|oxymoron|friendly reminder|"
    r"hot topic|tfw)\b",
    re.IGNORECASE,
)

SPAM_TERMS = {
    "follow me",
    "follow back",
    "like and repost",
    "like this",
    "repost this",
    "dm me",
    "check out my",
    "buy now",
    "limited offer",
    "promo code",
    "airdrop",
    "giveaway",
}

ENGAGEMENT_NEWS_RE = re.compile(
    r"\b(?:released|release[ds]?|announc(?:ed|es|ing)|launch(?:ed|es|ing)|"
    r"patch(?:ed|es|ing)?|update[ds]?|vulnerab(?:le|ility|ilities)|"
    r"breach(?:ed|es)?|flaw(?:ed|s)?|exploit(?:ed|s|ing)?|fixed|fixes|"
    r"researchers?|report(?:ed|s)?|warns?|warning|users?|customers?|"
    r"company|companies|CVE-\d|outage|leak(?:ed|s)?)\b",
    re.IGNORECASE,
)

TECH_SIGNAL_RE = re.compile(
    r"\b(?:AI|LLM|model|API|code|software|security|vulnerability|CVE|RCE|exploit|"
    r"password|kernel|Linux|SteamOS|Google|OpenAI|Microsoft|Apple|Nvidia|GPU|"
    r"cloud (?:computing|provider|providers|service|services|storage|platform|security)|"
    r"database|browser|app|developer|open source|patch|update|release|encryption|"
    r"privacy|data|server|chip|robot|automation)\b",
    re.IGNORECASE,
)

GENERIC_REPLY_PHRASES = {
    "great post",
    "nice post",
    "good post",
    "love this",
    "thanks for sharing",
    "this is huge",
    "this changes everything",
    "well said",
    "interesting",
    "check this out",
    "follow for more",
}

COMMENT_SYSTEM = """You are a knowledgeable developer replying on Bluesky.
Add one specific, useful observation to the supplied post.

Hard rules:
- Reply to the supplied post and stay grounded in it.
- You may add one sentence of well-established technical context, but do not invent numbers, named products, or claims.
- Do not give generic security advice; name the specific mechanism or consequence in the post.
- If the post is satire, a joke, or documentation rather than a real incident, return skip.
- 1 to 2 sentences, 35 to 190 characters.
- No praise, summary, generic reaction, question, link, mention, hashtag, emoji, or promotion.
- No first-person (I, we, our, my, me).
- Sound like a sharp human, not a brand. Do not mention being an AI or a bot.
- Add a specific implication, risk, or technical context; do not merely restate the post's numbers, versions, or headline.
- Do not repeat source URLs or domains from the post.
- If the post is only a release/version list with no substantive consequence, return {"skip": true, "reason": "..."}.
- If there is not enough substance for a specific reply, return {"skip": true, "reason": "..."}.

Return JSON only:
{"reply": "..."}
or
{"skip": true, "reason": "..."}"""


@dataclass(frozen=True)
class EngagementTarget:
    uri: str
    cid: str
    author_handle: str
    author_did: str
    text: str
    url: str
    likes: int
    reposts: int
    replies: int
    age_hours: float
    score: float

    @property
    def engagement(self) -> int:
        return self.likes + 2 * self.reposts + 3 * self.replies

    def to_dict(self) -> dict[str, object]:
        return {
            "uri": self.uri,
            "cid": self.cid,
            "author": self.author_handle,
            "author_did": self.author_did,
            "text": self.text,
            "url": self.url,
            "likes": self.likes,
            "reposts": self.reposts,
            "replies": self.replies,
            "age_hours": round(self.age_hours, 1),
            "engagement": self.engagement,
            "score": round(self.score, 2),
        }


@dataclass(frozen=True)
class ReplySuggestion:
    target: EngagementTarget
    reply: str
    provider: str

    def to_dict(self) -> dict[str, object]:
        payload = self.target.to_dict()
        payload["suggested_reply"] = self.reply
        payload["provider"] = self.provider
        return payload


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(default))
    if not isinstance(payload, dict):
        return json.loads(json.dumps(default))
    for key, value in default.items():
        payload.setdefault(key, json.loads(json.dumps(value)))
    return payload


def _save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_engagement_state(path: Path) -> dict[str, Any]:
    state = _load_json(path, DEFAULT_ENGAGEMENT_STATE)
    if not isinstance(state.get("replies"), list):
        state["replies"] = []
    return state


def save_engagement_state(path: Path, state: dict[str, Any]) -> None:
    _save_json(path, state)


def load_growth_state(path: Path) -> dict[str, Any]:
    state = _load_json(path, DEFAULT_GROWTH_STATE)
    if not isinstance(state.get("snapshots"), list):
        state["snapshots"] = []
    return state


def save_growth_state(path: Path, state: dict[str, Any]) -> None:
    _save_json(path, state)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recent_replies(state: dict[str, Any], now: datetime, days: int) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    values: list[dict[str, Any]] = []
    for entry in state.get("replies", []):
        posted_at = _parse_datetime(entry.get("posted_at"))
        if posted_at and posted_at >= cutoff:
            values.append(entry)
    return values


def can_reply_now(
    state: dict[str, Any],
    now: datetime,
    settings: Settings,
    *,
    author_did: str,
    target_uri: str,
) -> tuple[bool, str]:
    replies = _recent_replies(state, now, 30)
    if any(entry.get("target_uri") == target_uri for entry in replies):
        return False, "already replied to this post"

    daily = _recent_replies(state, now, 1)
    if len(daily) >= settings.engagement_daily_limit:
        return False, f"daily reply cap reached ({settings.engagement_daily_limit})"
    weekly = _recent_replies(state, now, 7)
    if len(weekly) >= settings.engagement_weekly_limit:
        return False, f"weekly reply cap reached ({settings.engagement_weekly_limit})"

    cooldown = now - timedelta(days=settings.engagement_author_cooldown_days)
    for entry in state.get("replies", []):
        if entry.get("author_did") != author_did:
            continue
        posted_at = _parse_datetime(entry.get("posted_at"))
        if posted_at and posted_at >= cooldown:
            return False, "author replied to recently"
    return True, "ok"


def record_reply(
    state: dict[str, Any],
    *,
    target: EngagementTarget,
    reply_text: str,
    reply_uri: str,
    reply_cid: str,
    posted_at: datetime,
) -> None:
    state.setdefault("replies", []).append(
        {
            "target_uri": target.uri,
            "target_cid": target.cid,
            "author": target.author_handle,
            "author_did": target.author_did,
            "target_text": target.text,
            "reply_text": reply_text,
            "reply_uri": reply_uri,
            "reply_cid": reply_cid,
            "posted_at": posted_at.isoformat(),
        }
    )
    state["last_run"] = posted_at.isoformat()


def _post_url(handle: str, uri: str) -> str:
    rkey = uri.rsplit("/", 1)[-1]
    if not handle or not rkey:
        return ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _post_age(record: dict[str, Any], now: datetime) -> float | None:
    created = _parse_datetime(record.get("createdAt") or record.get("indexedAt"))
    if not created:
        return None
    return max(0.0, (now - created).total_seconds() / 3600)


def _target_from_post(
    post: dict[str, Any],
    now: datetime,
    query: str,
    settings: Settings,
) -> EngagementTarget | None:
    record = post.get("record") or {}
    author = post.get("author") or {}
    text = normalize_space(str(record.get("text") or ""))
    uri = str(post.get("uri") or "")
    cid = str(post.get("cid") or "")
    author_did = str(author.get("did") or "")
    author_handle = str(author.get("handle") or "")
    if not uri or not cid or not author_did or not text:
        return None
    if record.get("reply"):
        return None
    if record.get("embed") or post.get("embed"):
        return None
    if len(text) < 120 or len(text) > 700:
        return None
    langs = [str(lang).lower() for lang in (record.get("langs") or []) if lang]
    if langs and "en" not in langs:
        return None
    if not langs:
        non_ascii = sum(1 for char in text if ord(char) > 127)
        if non_ascii / max(len(text), 1) > 0.12:
            return None
    if text.startswith(('"', "“", "'")):
        return None
    if FIRST_PERSON_RE.search(text):
        return None
    if "@" in text or not TECH_SIGNAL_RE.search(text):
        return None
    if not ENGAGEMENT_NEWS_RE.search(text):
        return None
    age = _post_age(record, now)
    if age is None or age > settings.engagement_max_age_hours:
        return None
    likes = int(post.get("likeCount") or 0)
    reposts = int(post.get("repostCount") or 0)
    replies = int(post.get("replyCount") or 0)
    if likes < settings.engagement_min_likes or replies > settings.engagement_max_replies:
        return None
    lowered = text.lower()
    if is_disallowed_text(text) or ENGAGEMENT_BLOCKED_RE.search(text):
        return None
    if ENGAGEMENT_OPINION_RE.search(text):
        return None
    if any(term in lowered for term in SPAM_TERMS):
        return None

    engagement = likes + 2 * reposts + 3 * replies
    engagement_score = min(45.0, math.log10(engagement + 1) * 18)
    recency_score = max(0.0, 24.0 - age * 0.55)
    query_tokens = {token for token in re.findall(r"[a-z0-9]+", query.lower()) if len(token) > 2}
    text_tokens = set(re.findall(r"[a-z0-9]+", lowered))
    if query_tokens and not (query_tokens & text_tokens):
        return None
    relevance = 10.0 if not query_tokens else min(14.0, 4.0 + len(query_tokens & text_tokens) * 2.0)
    reply_gap = 6.0 if replies <= 10 else 0.0
    score = engagement_score + recency_score + relevance + reply_gap
    return EngagementTarget(
        uri=uri,
        cid=cid,
        author_handle=author_handle,
        author_did=author_did,
        text=text,
        url=_post_url(author_handle, uri),
        likes=likes,
        reposts=reposts,
        replies=replies,
        age_hours=age,
        score=score,
    )


def find_targets(
    client: BlueskyClient,
    settings: Settings,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    per_query: int = 50,
) -> list[EngagementTarget]:
    now = now or datetime.now(timezone.utc)
    topics = [topic.strip() for topic in settings.engagement_topics.split(",") if topic.strip()]
    seen_uris: set[str] = set()
    targets: list[EngagementTarget] = []
    for topic in topics[:20]:
        for sort in ("top", "latest"):
            try:
                posts = client.search_posts(topic, limit=per_query, sort=sort)
            except Exception as exc:
                logger.warning(
                    "engagement search failed for %s (%s): %s", topic, sort, exc
                )
                continue
            for post in posts:
                target = _target_from_post(post, now, topic, settings)
                if not target or target.uri in seen_uris:
                    continue
                if target.author_did == client.did:
                    continue
                allowed, _ = can_reply_now(
                    state,
                    now,
                    settings,
                    author_did=target.author_did,
                    target_uri=target.uri,
                )
                if not allowed:
                    continue
                seen_uris.add(target.uri)
                targets.append(target)
    targets.sort(key=lambda target: target.score, reverse=True)
    return targets


def _valid_comment(reply: str, target: str) -> tuple[bool, str]:
    reply = normalize_space(reply)
    if not (35 <= len(reply) <= 190):
        return False, "reply length outside 35-190"
    lowered = reply.lower()
    if any(phrase in lowered for phrase in GENERIC_REPLY_PHRASES):
        return False, "generic praise/reaction"
    if "http://" in lowered or "https://" in lowered:
        return False, "link in reply"
    if "@" in reply:
        return False, "mention in reply"
    if "#" in reply:
        return False, "hashtag in reply"
    if "?" in reply:
        return False, "question in reply"
    if FIRST_PERSON_RE.search(reply):
        return False, "first-person language"
    if is_disallowed_text(reply):
        return False, "blocked term in reply"
    sentences = [part for part in re.split(r"(?<=[.!])\\s+", reply) if part]
    if not (1 <= len(sentences) <= 2):
        return False, "expected 1-2 sentences"

    stop = {
        "about", "after", "again", "also", "because", "been", "before", "being",
        "could", "does", "from", "have", "into", "just", "more", "most", "much",
        "only", "over", "same", "should", "some", "such", "than", "that", "their",
        "them", "then", "there", "these", "they", "this", "those", "through",
        "under", "very", "what", "when", "where", "which", "while", "with",
        "would", "your", "this", "will", "cant", "dont",
    }
    target_tokens = {
        token for token in re.findall(r"[a-z0-9]+", target.lower())
        if len(token) >= 4 and token not in stop
    }
    reply_tokens = {
        token for token in re.findall(r"[a-z0-9]+", lowered)
        if len(token) >= 4 and token not in stop
    }
    shared = set(target_tokens & reply_tokens)
    for target_token in target_tokens - shared:
        for reply_token in reply_tokens - shared:
            if (
                len(target_token) >= 5
                and len(reply_token) >= 5
                and (target_token in reply_token or reply_token in target_token)
            ):
                shared.update({target_token, reply_token})
                break
    numbers = re.findall(r"\\b\\d+(?:\\.\\d+)*\\b", reply)
    if len(shared) < 2 and not numbers:
        return False, "reply is not grounded in the target post"
    if len(shared) > 12:
        return False, "reply copies too much of the target"
    domains = set(re.findall(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", target.lower()))
    if any(domain in lowered for domain in domains):
        return False, "reply repeats a source domain"
    if any(phrase in lowered for phrase in ("notable bump", "notable release", "notable change")):
        return False, "generic release commentary"
    return True, "ok"


def suggest_reply(
    settings: Settings,
    target: EngagementTarget,
) -> tuple[ReplySuggestion | None, str, str]:
    payload, provider = generate_json(
        settings,
        COMMENT_SYSTEM,
        "Reply to this Bluesky post:\n" + json.dumps(
            {
                "author": target.author_handle,
                "text": target.text,
                "likes": target.likes,
                "reposts": target.reposts,
                "replies": target.replies,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    if not payload:
        return None, "ai_failed", provider
    if payload.get("skip"):
        return None, str(payload.get("reason") or "AI requested skip"), provider
    reply = normalize_space(str(payload.get("reply") or ""))
    ok, reason = _valid_comment(reply, target.text)
    if not ok:
        return None, reason, provider
    return ReplySuggestion(target=target, reply=reply, provider=provider), "ok", provider


def plan_engagement(
    client: BlueskyClient,
    settings: Settings,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    limit: int = 5,
    generate: bool = True,
) -> dict[str, Any]:
    targets = find_targets(client, settings, state, now=now)
    suggestions: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    if generate and targets:
        if not any(
            (settings.gemini_api_key, settings.groq_api_key, settings.openrouter_api_key)
        ):
            return {
                "status": "ai_required",
                "reason": "Set GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY for quality auto-replies.",
                "targets": [target.to_dict() for target in targets[:limit]],
            }
        for target in targets[: max(limit, 1)]:
            suggestion, reason, provider = suggest_reply(settings, target)
            if suggestion:
                suggestions.append(suggestion.to_dict())
            else:
                skipped.append({"uri": target.uri, "reason": reason, "provider": provider})
            if len(suggestions) >= limit:
                break
    return {
        "status": "engagement_plan",
        "targets": [target.to_dict() for target in targets[:limit]],
        "suggestions": suggestions,
        "skipped": skipped,
        "count": len(targets),
    }


def run_engagement(
    client: BlueskyClient,
    settings: Settings,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not settings.engagement_enabled:
        return {
            "status": "disabled",
            "reason": "ENGAGEMENT_ENABLED is false",
        }
    if not settings.posting_enabled:
        return {
            "status": "disabled",
            "reason": "POSTING_ENABLED is false; comments are disabled too",
        }
    if settings.engagement_require_ai and not any(
        (settings.gemini_api_key, settings.groq_api_key, settings.openrouter_api_key)
    ):
        return {
            "status": "ai_required",
            "reason": "Set an AI key for quality auto-replies; refusing generic comments.",
        }

    allowed, reason = can_reply_now(
        state,
        now,
        settings,
        author_did="__none__",
        target_uri="__none__",
    )
    if not allowed:
        return {"status": "skipped", "reason": reason}

    targets = find_targets(client, settings, state, now=now)
    if not targets:
        return {"status": "no_target", "reason": "no relevant posts passed filters"}

    posted = 0
    skipped: list[dict[str, str]] = []
    for target in targets:
        if posted >= settings.engagement_per_run:
            break
        suggestion, reason, provider = suggest_reply(settings, target)
        if not suggestion:
            skipped.append({"uri": target.uri, "reason": reason, "provider": provider})
            continue
        root = {"uri": target.uri, "cid": target.cid}
        result = client.create_reply(
            text=suggestion.reply,
            parent_uri=target.uri,
            parent_cid=target.cid,
            root_uri=root["uri"],
            root_cid=root["cid"],
            created_at=now,
        )
        reply_uri = str(result.get("uri") or "")
        reply_cid = str(result.get("cid") or "")
        if reply_uri:
            record_reply(
                state,
                target=target,
                reply_text=suggestion.reply,
                reply_uri=reply_uri,
                reply_cid=reply_cid,
                posted_at=now,
            )
            posted += 1

    if not posted:
        return {
            "status": "no_comment",
            "reason": "AI replies failed relevance/quality checks",
            "skipped": skipped,
        }
    return {
        "status": "commented",
        "posted": posted,
        "skipped": skipped,
        "last_reply": state["replies"][-1],
    }


def _snapshot(profile: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "at": now.isoformat(),
        "followers": int(profile.get("followersCount") or 0),
        "following": int(profile.get("followsCount") or 0),
        "posts": int(profile.get("postsCount") or 0),
    }


def build_growth_report(
    client: BlueskyClient,
    handle: str,
    *,
    previous: dict[str, Any] | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    profile = client.get_profile(handle)
    feed = client.get_author_feed(handle, limit=limit, filter="posts_no_replies")
    did = str(profile.get("did") or client.did or "")
    posts: list[dict[str, Any]] = []
    for item in feed.get("feed", []):
        post = item.get("post") or {}
        author = post.get("author") or {}
        if author.get("did") != did:
            continue
        record = post.get("record") or {}
        likes = int(post.get("likeCount") or 0)
        reposts = int(post.get("repostCount") or 0)
        replies = int(post.get("replyCount") or 0)
        uri = str(post.get("uri") or "")
        posts.append(
            {
                "uri": uri,
                "url": _post_url(handle, uri),
                "text": normalize_space(str(record.get("text") or "")),
                "created_at": record.get("createdAt"),
                "likes": likes,
                "reposts": reposts,
                "replies": replies,
                "engagement": likes + 2 * reposts + 3 * replies,
            }
        )
    total_likes = sum(post["likes"] for post in posts)
    total_reposts = sum(post["reposts"] for post in posts)
    total_replies = sum(post["replies"] for post in posts)
    total_engagement = total_likes + 2 * total_reposts + 3 * total_replies
    followers = int(profile.get("followersCount") or 0)
    previous_followers = 0
    if previous:
        snapshots = previous.get("snapshots") or []
        if snapshots:
            previous_followers = int(snapshots[-1].get("followers") or 0)
    top_posts = sorted(posts, key=lambda post: post["engagement"], reverse=True)[:5]
    snapshot = _snapshot(profile, now)
    return {
        "status": "growth_report",
        "handle": handle,
        "did": did,
        "followers": followers,
        "following": int(profile.get("followsCount") or 0),
        "posts": int(profile.get("postsCount") or 0),
        "followers_delta": followers - previous_followers if previous_followers else 0,
        "analyzed_posts": len(posts),
        "total_likes": total_likes,
        "total_reposts": total_reposts,
        "total_replies": total_replies,
        "average_engagement": round(total_engagement / len(posts), 2) if posts else 0,
        "engagement_rate_pct": round(
            (total_engagement / len(posts)) / max(followers, 1) * 100, 2
        ) if posts else 0,
        "top_posts": top_posts,
        "snapshot": snapshot,
    }
