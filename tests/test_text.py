from app.models import SourceRef
from app.text import (
    canonical_url,
    clean_body,
    short_url,
    source_line,
    title_fingerprint,
)


def test_canonical_url_strips_tracking_and_www():
    url = "https://www.php.net/security/?utm_source=x&fbclid=y#top"
    assert canonical_url(url) == "https://php.net/security"


def test_short_url_stays_readable():
    assert short_url("https://www.php.net/security/") == "php.net/security"


def test_source_line_one_and_two_sources():
    one = [SourceRef("PHP.net", "https://www.php.net/security")]
    assert source_line(one) == "Source: PHP.net — php.net/security"
    two = [
        SourceRef("NVD", "https://nvd.nist.gov/vuln/detail/CVE-2026-109618"),
        SourceRef("CISA KEV", "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"),
    ]
    assert source_line(two) == "Sources: NVD, CISA KEV"


def test_clean_body_removes_source_urls_hashtags_and_emoji():
    raw = "Patch now. Source: PHP.net — php.net/security https://php.net #PHP 🔥"
    cleaned = clean_body(raw)
    assert "Source:" not in cleaned
    assert "http" not in cleaned
    assert "#PHP" not in cleaned
    assert "🔥" not in cleaned


def test_title_fingerprint_ignores_common_words():
    assert title_fingerprint("The New PHP Release") == title_fingerprint("New PHP release")
