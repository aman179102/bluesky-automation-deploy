from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.growth import (
    _target_from_post,
    _valid_comment,
    build_growth_report,
    can_reply_now,
    load_engagement_state,
    record_reply,
    save_engagement_state,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(state_dir=tmp_path, output_dir=tmp_path, **overrides)


def test_comment_validation_accepts_specific_note():
    target = "The CVSS 9.4 flaw is being exploited and a patch is available."
    ok, reason = _valid_comment(
        "The CVSS 9.4 score makes this a patch-first issue for exposed hosts.",
        target,
    )
    assert ok, reason


def test_comment_validation_accepts_product_name_variant():
    target = 'The Brotli vulnerability is exploitable in Python and other consumers.'
    ok, reason = _valid_comment(
        'A single memory safety bug in libbrotli exposes every runtime using native bindings.',
        target,
    )
    assert ok, reason


def test_comment_validation_rejects_generic_or_promotional_text():
    target = "Microsoft released a Windows security update for CVE-2026-1234."
    for reply in (
        "Great post, thanks for sharing this.",
        "Check out my newsletter for more security news.",
        "I think this is a huge update. It changes everything.",
        "What does this mean for users?",
        "Pointing to man7.org documentation shows how credentials grant control.",
        "Linux 7.2 and Plasma 6.7 are notable bumps for this release.",
        "Relying on password-based authentication without key-based access or multi-factor checks leaves systems vulnerable to brute-force and credential stuffing attacks.",
        "Security matters for production systems.",
    ):
        ok, _ = _valid_comment(reply, target)
        assert not ok


def test_engagement_target_only_accepts_plain_text_posts(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = _settings(tmp_path)
    base = {
        "uri": "at://did:plc:alice/app.bsky.feed.post/1",
        "cid": "cid",
        "author": {"did": "did:plc:alice", "handle": "alice.test"},
        "record": {
            "text": (
                "Microsoft released a Windows security update for CVE-2026-1234 "
                "that fixes a remote code execution flaw in the exposed service."
            ),
            "langs": ["en"],
            "createdAt": (now - timedelta(hours=1)).isoformat(),
        },
        "likeCount": 100,
        "repostCount": 10,
        "replyCount": 2,
    }
    assert _target_from_post(base, now, "security", settings) is not None

    with_image = {
        **base,
        "record": {
            **base["record"],
            "embed": {"$type": "app.bsky.embed.images", "images": [{"alt": "x"}]},
        },
    }
    assert _target_from_post(with_image, now, "security", settings) is None

    with_link_card = {
        **base,
        "embed": {"$type": "app.bsky.embed.external#view", "external": {"uri": "https://x.test"}},
    }
    assert _target_from_post(with_link_card, now, "security", settings) is None

    with_reply = {
        **base,
        "record": {
            **base["record"],
            "reply": {"parent": {"uri": "at://did:plc:alice/app.bsky.feed.post/0"}},
        },
    }
    assert _target_from_post(with_reply, now, "security", settings) is None

    without_news = {
        **base,
        "record": {
            **base["record"],
            "text": "A thoughtful opinion about security and the technology industry overall.",
        },
    }
    assert _target_from_post(without_news, now, "security", settings) is None


def test_engagement_caps_and_author_cooldown(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = _settings(tmp_path, engagement_daily_limit=1, engagement_weekly_limit=2)
    state = load_engagement_state(tmp_path / "engagement.json")
    earlier = now - timedelta(hours=2)
    state["replies"].append(
        {
            "target_uri": "at://did/app.bsky.feed.post/one",
            "target_cid": "cid",
            "author": "alice.test",
            "author_did": "did:alice",
            "target_text": "A specific post about a Linux kernel patch.",
            "reply_text": "The kernel patch detail matters for exposed hosts.",
            "reply_uri": "at://did/app.bsky.feed.post/reply",
            "reply_cid": "cid",
            "posted_at": earlier.isoformat(),
        }
    )
    allowed, reason = can_reply_now(
        state,
        now,
        settings,
        author_did="did:bob",
        target_uri="at://did/app.bsky.feed.post/two",
    )
    assert not allowed
    assert "daily" in reason

    settings = _settings(tmp_path, engagement_daily_limit=5, engagement_weekly_limit=5)
    allowed, reason = can_reply_now(
        state,
        now,
        settings,
        author_did="did:alice",
        target_uri="at://did/app.bsky.feed.post/two",
    )
    assert not allowed
    assert "author" in reason


def test_engagement_state_round_trip(tmp_path: Path):
    path = tmp_path / "engagement.json"
    state = load_engagement_state(path)
    target = _target("did:alice", "alice.test")
    record_reply(
        state,
        target=target,
        reply_text="The patch detail matters for exposed hosts.",
        reply_uri="at://did/app.bsky.feed.post/reply",
        reply_cid="cid",
        posted_at=datetime.now(timezone.utc),
    )
    save_engagement_state(path, state)
    loaded = load_engagement_state(path)
    assert loaded["replies"][0]["reply_text"].startswith("The patch")


def test_target_filters_replies_language_and_politics():
    now = datetime.now(timezone.utc)
    settings = Settings()
    base = {
        "uri": "at://did/app.bsky.feed.post/abc",
        "cid": "cid",
        "author": {"did": "did:alice", "handle": "alice.test"},
        "record": {
            "text": (
                "Microsoft shipped a Windows security update for CVE-2026-1234 "
                "with a kernel fix, a mitigation for exposed servers, and new "
                "detection guidance for administrators."
            ),
            "createdAt": now.isoformat(),
            "langs": ["en"],
        },
        "likeCount": 20,
        "repostCount": 2,
        "replyCount": 1,
    }
    assert _target_from_post(base, now, "Microsoft", settings) is not None
    reply = {**base, "record": {**base["record"], "reply": {"root": {}, "parent": {}}}}
    assert _target_from_post(reply, now, "Microsoft", settings) is None
    portuguese = {**base, "record": {**base["record"], "langs": ["pt"]}}
    assert _target_from_post(portuguese, now, "Microsoft", settings) is None
    opinion = {
        **base,
        "record": {
            **base["record"],
            "text": (
                "Gonna be honest for a sec: a lot of people these days are "
                "giving Linux User derogatory energy when talking about computers."
            ),
        },
    }
    assert _target_from_post(opinion, now, "Linux", settings) is None
    activist = {
        **base,
        "record": {
            **base["record"],
            "text": (
                "After Google illegally hacked down the forest, please stop using "
                "Google and minimise your footprint with other search engines."
            ),
        },
    }
    assert _target_from_post(activist, now, "Google", settings) is None
    political = {
        **base,
        "record": {
            **base["record"],
            "text": "The attorney general campaign is attacking Microsoft over Windows.",
        },
    }
    assert _target_from_post(political, now, "Microsoft", settings) is None


def _target(did: str, handle: str):
    from app.growth import EngagementTarget

    return EngagementTarget(
        uri="at://did/app.bsky.feed.post/parent",
        cid="cid",
        author_handle=handle,
        author_did=did,
        text="A specific post about a Linux kernel patch.",
        url="https://bsky.app/profile/alice.test/post/parent",
        likes=20,
        reposts=2,
        replies=1,
        age_hours=2.0,
        score=50.0,
    )


class FakeClient:
    did = "did:me"

    def get_profile(self, handle: str):
        return {
            "did": self.did,
            "handle": handle,
            "followersCount": 10,
            "followsCount": 20,
            "postsCount": 2,
        }

    def get_author_feed(self, handle: str, *, limit: int, filter: str):
        post = {
            "uri": "at://did:me/app.bsky.feed.post/rkey",
            "author": {"did": self.did},
            "record": {"text": "A post", "createdAt": datetime.now(timezone.utc).isoformat()},
            "likeCount": 2,
            "repostCount": 1,
            "replyCount": 0,
        }
        return {"feed": [{"post": post}]}


def test_growth_report_metrics():
    report = build_growth_report(FakeClient(), "me.test")
    assert report["followers"] == 10
    assert report["analyzed_posts"] == 1
    assert report["average_engagement"] == 4
    assert report["top_posts"][0]["engagement"] == 4
