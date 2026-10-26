from datetime import datetime, timezone

from app.bluesky import (
    TID_ALPHABET,
    build_facets,
    make_tid,
    profile_has_bot_label,
    utc_timestamp,
)
from app.models import SourceRef


def test_facets_link_source_name_and_short_url():
    text = "PHP fixed two RCEs.\n#Security\nSource: PHP.net — php.net/security"
    refs = [SourceRef("PHP.net", "https://www.php.net/security")]
    facets = build_facets(text, refs)
    assert len(facets) == 2
    assert facets[0]["index"]["byteStart"] < facets[1]["index"]["byteStart"]
    assert all(
        facet["features"][0]["uri"] == "https://www.php.net/security" for facet in facets
    )


def test_facets_two_sources():
    text = "A critical flaw is exploited.\n#Security\nSources: NVD, CISA KEV"
    refs = [
        SourceRef("NVD", "https://nvd.nist.gov/vuln/detail/CVE-2026-109618"),
        SourceRef("CISA KEV", "https://www.cisa.gov/known-exploited-vulnerabilities-catalog"),
    ]
    facets = build_facets(text, refs)
    assert len(facets) == 2


def test_utc_timestamp():
    value = utc_timestamp(datetime(2026, 10, 25, 2, 37, tzinfo=timezone.utc))
    assert value == "2026-10-25T02:37:00.000Z"


def test_tid_is_valid_and_sortable():
    early = make_tid(datetime(2026, 10, 25, 2, 37, tzinfo=timezone.utc), 1)
    late = make_tid(datetime(2026, 10, 25, 2, 37, 0, 1, tzinfo=timezone.utc), 1)
    assert len(early) == 13
    assert all(char in TID_ALPHABET for char in early)
    assert early < late


def test_bot_label_requires_self_labels_type():
    assert profile_has_bot_label(
        {
            "labels": {
                "$type": "com.atproto.label.defs#selfLabels",
                "values": [{"val": "bot"}],
            }
        }
    )
    assert not profile_has_bot_label(
        {
            "labels": {
                "$type": "com.atproto.label.defs",
                "values": [{"val": "bot"}],
            }
        }
    )
