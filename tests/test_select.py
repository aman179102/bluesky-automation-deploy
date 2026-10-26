from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.models import StoryItem
from app.select import rank_candidates, select_story


def _item(title: str, summary: str, *, cve: str, source: str = "NVD") -> StoryItem:
    return StoryItem(
        source=source,
        title=title,
        url=f"https://nvd.nist.gov/vuln/detail/{cve}",
        summary=summary,
        category="security",
        published_at=datetime.now(timezone.utc),
        extra={"cisa": True, "cve": cve, "cvss": 9.4},
    )


def test_niche_story_is_rejected_before_selection(tmp_path: Path):
    now = datetime.now(timezone.utc)
    settings = Settings(state_dir=tmp_path, output_dir=tmp_path)
    niche = _item(
        "Arista VeloCloud Orchestrator: CVE-2026-114625 added to CISA exploited list",
        "A critical flaw in Arista VeloCloud Orchestrator is being exploited. CISA set a deadline.",
        cve="CVE-2026-114625",
    )
    broad = _item(
        "Microsoft Windows CVE-2026-1234 added to CISA exploited list",
        "A critical Windows flaw is being exploited. Patch now.",
        cve="CVE-2026-1234",
    )
    candidates = rank_candidates([niche, broad], {"posts": [], "seen": []}, now, settings)
    by_title = {candidate.item.title: candidate for candidate in candidates}
    assert not by_title[niche.title].eligible
    assert "virality" in by_title[niche.title].reason
    assert by_title[broad.title].eligible
    story = select_story([niche, broad], {"posts": [], "seen": []}, now, settings)
    assert story is not None
    assert story.item.title == broad.title
