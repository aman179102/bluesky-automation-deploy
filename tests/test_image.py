from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from app.config import Settings
from app.image import _hero_parts, render_image
from app.models import SourceRef, Story, StoryItem
from app.writer import Writer


def test_render_card(tmp_path):
    story = Story(
        item=StoryItem(
            source="PHP.net",
            title="PHP 8.5.12, 8.4.27, 8.3.36 and 8.2.35 released",
            url="https://www.php.net/security",
            summary=(
                "PHP released emergency patches for two critical RCEs, CVE-2026-109618 "
                "and CVE-2026-109619. Fixed in 8.5.12, 8.4.27, 8.3.36 and 8.2.35."
            ),
            category="security",
            published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        ),
        sources=[SourceRef("PHP.net", "https://www.php.net/security")],
        facts={"cvss": 9.2},
    )
    settings = Settings(state_dir=tmp_path, output_dir=tmp_path)
    draft = Writer(settings, allow_ai=False).draft(story)
    output = render_image(draft, story, tmp_path / "card.jpg")
    assert output.exists()
    assert output.stat().st_size < 2_000_000
    with Image.open(output) as image:
        assert image.size == (1200, 675)
        assert image.format == "JPEG"


def test_hero_parts_extracts_scannable_number():
    assert _hero_parts("CVSS 9.4") == ("CVSS", "9.4", 72)
    assert _hero_parts("Price: $0") == ("PRICE", "$0", 62)
    assert _hero_parts("Due 2026-10-26") == ("PATCH BY", "2026-10-26", 52)
    assert _hero_parts("100,000 advertisers") == ("ADVERTISERS", "100,000", 62)
    assert _hero_parts("Free tier includes GPT-5.") == (
        "KEY DETAIL",
        "Free tier includes GPT-5.",
        38,
    )
