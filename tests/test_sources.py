from datetime import datetime, timezone

from app.sources import (
    FeedSpec,
    classify_category,
    extract_article_text,
    parse_feed,
    parse_kev_payload,
)

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>PHP 8.5.12 released</title>
    <link rel="alternate" href="https://www.php.net/archive/2026.php"/>
    <published>2026-10-21T06:05:00Z</published>
    <summary>PHP released emergency security patches for two critical RCEs.</summary>
    <id>php-8.5.12</id>
  </entry>
</feed>
"""


def test_parse_atom_feed():
    spec = FeedSpec("PHP.net", "https://www.php.net/feed.atom", "dev", 1.0)
    items = parse_feed(ATOM, spec, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert len(items) == 1
    item = items[0]
    assert item.title == "PHP 8.5.12 released"
    assert item.url == "https://php.net/archive/2026.php"
    assert item.category == "security"
    assert item.source == "PHP.net"


def test_parse_kev_payload():
    payload = {
        "vulnerabilities": [
            {
                "cveID": "CVE-2026-109618",
                "vendorProject": "PHP",
                "product": "PHP",
                "vulnerabilityName": "PHP Remote Code Execution",
                "shortDescription": "A critical remote code execution flaw affects PHP web requests.",
                "requiredAction": "Apply the vendor update.",
                "dueDate": "2026-11-10",
                "knownRansomwareCampaignUse": "Unknown",
                "dateAdded": "2026-10-21",
            }
        ]
    }
    items = parse_kev_payload(payload, now=datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert len(items) == 1
    item = items[0]
    assert item.extra["cisa"] is True
    assert item.extra["cve"] == "CVE-2026-109618"
    assert item.url.endswith("CVE-2026-109618")
    assert item.category == "security"


def test_classify_category():
    assert classify_category("New AI model", "") == "ai"
    assert classify_category("Critical CVE in PHP", "") == "security"
    assert classify_category("Go 1.26 released", "") == "dev"
    assert (
        classify_category(
            "Updated Google privacy policy says your Gmail might be used for advertising",
            "Turning on Personal Intelligence gives the company more data for ad targeting.",
            "tech",
        )
        == "bigtech"
    )


def test_extract_article_text_skips_navigation_and_script():
    html = """
    <html><body>
      <nav><p>This navigation paragraph should be skipped entirely.</p></nav>
      <script>const text = "This script should also be skipped";</script>
      <p>Adding a new SIM to your phone links it to all Google accounts on that device.</p>
      <p>This setting has been in place by default since September 2025.</p>
    </body></html>
    """
    text = extract_article_text(html)
    assert "navigation paragraph" not in text
    assert "script" not in text.lower()
    assert "Adding a new SIM" in text
    assert "September 2025" in text
