from datetime import datetime, timezone

from app.models import StoryItem
from app.virality import brand_strength, is_mainstream_target, score_virality


def _item(title: str, summary: str, source: str = "The Verge", category: str = "tech") -> StoryItem:
    return StoryItem(
        source=source,
        title=title,
        url="https://www.theverge.com/story",
        summary=summary,
        category=category,
        published_at=datetime.now(timezone.utc),
    )


def test_niche_cisa_story_is_suppressed():
    item = _item(
        "Arista VeloCloud Orchestrator: CVE-2026-114625 added to CISA exploited list",
        "A critical flaw is being exploited. CISA set a federal deadline.",
        source="NVD",
        category="security",
    )
    result = score_virality(item)
    assert result.score < 20
    assert result.audience == "niche"
    assert not is_mainstream_target("Arista VeloCloud Orchestrator")


def test_mainstream_security_story_scores_high():
    item = _item(
        "Microsoft Windows CVE-2026-1234 added to CISA exploited list",
        "A critical Windows flaw is being exploited. Patch now.",
        source="NVD",
        category="security",
    )
    result = score_virality(item)
    assert result.score >= 40
    assert is_mainstream_target("Microsoft Windows")
    assert brand_strength("Microsoft Windows") >= 24


def test_consumer_ai_story_gets_broad_audience():
    item = _item(
        "OpenAI makes ChatGPT free for everyone",
        "OpenAI says the free tier now includes GPT-5 for all users.",
    )
    result = score_virality(item)
    assert result.score >= 40
    assert result.audience in {"mass", "tech"}
    assert any("openai" in reason for reason in result.reasons)
