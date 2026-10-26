from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from app.compliance import (
    can_post_now,
    filter_item,
    is_allowed_source,
    validate_image,
    validate_text,
)
from app.config import Settings
from app.models import PostDraft, SourceRef, Story, StoryItem
from app.writer import Writer


def _settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, output_dir=tmp_path)


def _story() -> Story:
    item = StoryItem(
        source="PHP.net",
        title="PHP security release",
        url="https://www.php.net/security",
        summary=(
            "PHP released emergency patches for two critical RCEs, CVE-2026-109618 "
            "and CVE-2026-109619. Fixed in 8.5.12, 8.4.27, 8.3.36 and 8.2.35."
        ),
        category="security",
        published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    return Story(item=item, sources=[SourceRef("PHP.net", item.url)])


def _draft(story: Story, tmp_path: Path) -> PostDraft:
    return Writer(_settings(tmp_path), allow_ai=False).draft(story)


def test_good_draft_passes(tmp_path):
    story = _story()
    assert validate_text(_draft(story, tmp_path), story, _settings(tmp_path)) == []


def test_clickbait_and_first_person_fail(tmp_path):
    story = _story()
    clean = _draft(story, tmp_path)
    bad = PostDraft(
        text=clean.text.replace(clean.body, "I think this is a huge deal. It changes everything."),
        body="I think this is a huge deal. It changes everything.",
        source_refs=clean.source_refs,
        image_title=clean.image_title,
        image_facts=clean.image_facts,
        alt_text=clean.alt_text,
        category=clean.category,
    )
    errors = validate_text(bad, story, _settings(tmp_path))
    assert any("first-person" in error for error in errors)
    assert any("clickbait" in error or "sentences" in error for error in errors)


def test_sources_plural_line_is_recognized(tmp_path):
    story = _story()
    clean = _draft(story, tmp_path)
    text = clean.text.replace("Source: PHP.net", "Sources: PHP.net, NVD")
    draft = PostDraft(
        text=text,
        body=clean.body,
        source_refs=[
            SourceRef("PHP.net", "https://www.php.net/security"),
            SourceRef("NVD", "https://nvd.nist.gov/vuln/detail/CVE-2026-109618"),
        ],
        image_title=clean.image_title,
        image_facts=clean.image_facts,
        alt_text=clean.alt_text,
        category=clean.category,
    )
    assert not any("missing source line" in error for error in validate_text(draft, story, _settings(tmp_path)))


def test_non_news_filters_listicles_and_marketing():
    listicle = StoryItem(
        source="Wired",
        title="6 Best Folding Phones (2026): Samsung, Apple, Google, Motorola",
        url="https://www.wired.com/story/best-folding-phones",
        summary="We tested the best folding phones for battery life, cameras, and durability.",
        category="hardware",
    )
    marketing = StoryItem(
        source="OpenAI",
        title="ChatGPT Ads expands to 26 new countries as over 100,000 advertisers have used the platform",
        url="https://openai.com/index/chatgpt-ads-expands-26-new-countries",
        summary="From local businesses to global brands, more advertisers can now reach people as they explore.",
        category="ai",
    )
    assert filter_item(listicle) == "non-news content"
    assert filter_item(marketing) == "non-news content"


def test_source_allowlist():
    assert is_allowed_source("https://www.php.net/security")
    assert not is_allowed_source("https://reddit.com/r/php")
    assert not is_allowed_source("https://bit.ly/abc")


def test_image_validation(tmp_path):
    path = tmp_path / "card.jpg"
    Image.new("RGB", (1200, 675), (10, 20, 30)).save(path, format="JPEG", quality=90)
    assert validate_image(path) == []


def test_can_post_now_windows_and_caps(tmp_path):
    settings = _settings(tmp_path)
    morning = datetime(2026, 10, 25, 2, 37, tzinfo=timezone.utc)  # 08:07 IST
    afternoon = datetime(2026, 10, 25, 7, 37, tzinfo=timezone.utc)  # 13:07 IST
    state = {"posts": []}
    assert can_post_now(state, morning, False, settings)[0] is True
    assert can_post_now(state, afternoon, False, settings)[0] is False
    assert can_post_now(state, afternoon, True, settings)[0] is True

    state["posts"] = [
        {"posted_at": (morning - timedelta(hours=1)).isoformat()},
        {"posted_at": morning.isoformat()},
    ]
    assert can_post_now(state, afternoon, False, settings)[0] is False
    assert can_post_now(state, afternoon, True, settings)[0] is True
    state["posts"].append({"posted_at": (morning + timedelta(hours=1)).isoformat()})
    state["posts"].append({"posted_at": (morning + timedelta(hours=2)).isoformat()})
    assert can_post_now(state, afternoon, True, settings)[0] is False
    assert can_post_now(state, afternoon, True, settings, ignore_window=True)[0] is False


def test_breaking_daily_and_weekly_caps(tmp_path):
    settings = _settings(tmp_path)
    morning = datetime(2026, 10, 25, 2, 37, tzinfo=timezone.utc)
    state = {"posts": [{"posted_at": morning.isoformat(), "breaking": True}]}
    assert can_post_now(state, morning + timedelta(hours=5), True, settings)[0] is False

    state = {
        "posts": [
            {
                "posted_at": (morning - timedelta(days=offset)).isoformat(),
                "breaking": True,
            }
            for offset in (1, 2, 3)
        ]
    }
    assert can_post_now(state, morning, True, settings)[0] is False
