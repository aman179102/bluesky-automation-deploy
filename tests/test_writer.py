from datetime import datetime, timezone
from pathlib import Path

from app.compliance import validate_text
from app.config import Settings
from app.models import SourceRef, Story, StoryItem
from app.writer import Writer


def _settings(tmp_path: Path) -> Settings:
    return Settings(state_dir=tmp_path, output_dir=tmp_path)


def _php_story() -> Story:
    item = StoryItem(
        source="PHP.net",
        title="PHP 8.5.12, 8.4.27, 8.3.36 and 8.2.35 released",
        url="https://www.php.net/security",
        summary=(
            "PHP released emergency patches for two critical remote code execution "
            "vulnerabilities, CVE-2026-109618 and CVE-2026-109619. PHP.net says mod_php "
            "is the highest-risk setup. Fixed in 8.5.12, 8.4.27, 8.3.36 and 8.2.35."
        ),
        category="security",
        published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    return Story(
        item=item,
        sources=[SourceRef("PHP.net", "https://www.php.net/security")],
        facts={"cvss": 9.2, "severity": "CRITICAL"},
    )


def test_template_draft_is_crisp_and_compliant(tmp_path):
    settings = _settings(tmp_path)
    story = _php_story()
    draft = Writer(settings, allow_ai=False).draft(story)
    assert draft.provider == "template"
    assert 190 <= len(draft.text) <= 300
    assert "Source: PHP.net" in draft.text
    assert "CVE-2026-109618" in draft.text
    assert "8.5.12" in draft.text
    assert "I " not in draft.body
    assert 3 <= len(draft.body.split(". ")) <= 5
    assert not validate_text(draft, story, settings)
    assert draft.image_title
    assert 1 <= len(draft.image_facts) <= 3


def test_phone_linking_story_gets_non_duplicate_image_fact(tmp_path):
    item = StoryItem(
        source="Engadget",
        title="Google is quietly linking your phone number to other Android accounts",
        url="https://www.engadget.com/2292018/google-quietly-linking-phone-number-to-android-accounts",
        summary="Adding a new SIM to your phone links it to all Google accounts.",
        category="bigtech",
        published_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    story = Story(
        item=item,
        sources=[SourceRef("Engadget", item.url)],
        facts={
            "article_text": (
                "Adding a new SIM to your phone links it to all Google accounts. "
                "This setting has been in place by default since September 2025."
            )
        },
    )
    draft = Writer(_settings(tmp_path), allow_ai=False).draft(story)
    assert draft.image_title == "Google: Phone number linked to Android accounts"
    assert "Phone number linked to Android accounts" not in draft.image_facts
    assert "On by default since September 2025" in draft.image_facts


def test_generic_privacy_story_uses_impact_and_clean_image_fact(tmp_path):
    item = StoryItem(
        source="Ars Technica",
        title="Updated Google privacy policy says your Gmail might be used for advertising",
        url="https://arstechnica.com/gadgets/2026/10/gmail-privacy-policy",
        summary="Turning on Personal Intelligence gives the company more data for ad targeting.",
        category="bigtech",
        published_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
    )
    story = Story(item=item, sources=[SourceRef("Ars Technica", item.url)])
    draft = Writer(_settings(tmp_path), allow_ai=False).draft(story)
    assert "That could change which ads Gmail users see." in draft.body
    assert "Pricing, specifications" not in draft.body
    assert draft.image_title == "Google: Gmail may be used for ads"
    assert "Gmail may be used for ads" not in draft.image_facts
    assert "More data for ad targeting" in draft.image_facts


def test_cisa_template_is_compliant(tmp_path):
    item = StoryItem(
        source="NVD",
        title="PHP PHP: CVE-2026-109618 added to CISA exploited list",
        url="https://nvd.nist.gov/vuln/detail/CVE-2026-109618",
        summary="A critical remote code execution flaw affects PHP web requests.",
        category="security",
        published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    story = Story(
        item=item,
        sources=[
            SourceRef("NVD", item.url),
            SourceRef(
                "CISA KEV",
                "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
            ),
        ],
        facts={
            "cisa": True,
            "cve": "CVE-2026-109618",
            "vendor": "PHP",
            "product": "PHP",
            "cvss": 9.2,
            "due_date": "2026-11-10",
            "nvd_description": "A critical remote code execution flaw affects PHP web requests.",
        },
    )
    draft = Writer(_settings(tmp_path), allow_ai=False).draft(story)
    assert 190 <= len(draft.text) <= 300
    assert "Sources: NVD, CISA KEV" in draft.text
    assert "PHP PHP" not in draft.text
    assert "this issue" not in draft.text
    assert "…" not in draft.text
    assert "remediation deadline" in draft.text
    assert draft.body.startswith("If your organization runs PHP")
    assert "CISA confirms active exploitation." in draft.body
    assert not validate_text(draft, story, _settings(tmp_path))


def test_cisa_mainstream_uses_reader_first_hook(tmp_path):
    item = StoryItem(
        source="NVD",
        title="Microsoft Windows: CVE-2026-22334 added to CISA exploited list",
        url="https://nvd.nist.gov/vuln/detail/CVE-2026-22334",
        summary="A critical Windows flaw is being exploited in the wild.",
        category="security",
        published_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
    )
    story = Story(
        item=item,
        sources=[SourceRef("NVD", item.url)],
        facts={
            "cisa": True,
            "cve": "CVE-2026-22334",
            "vendor": "Microsoft",
            "product": "Windows",
            "cvss": 9.8,
            "severity": "CRITICAL",
            "due_date": "2026-11-10",
        },
    )
    draft = Writer(_settings(tmp_path), allow_ai=False).draft(story)
    assert draft.body.startswith("If you use Microsoft Windows")
    assert "patch CVE-2026-22334 now" in draft.body
    assert not validate_text(draft, story, _settings(tmp_path))
