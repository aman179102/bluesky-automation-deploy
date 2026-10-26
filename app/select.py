from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from app.compliance import filter_item
from app.config import Settings
from app.models import Story, StoryItem
from app.sources import build_story
from app.state import recent_categories, recent_sources, was_seen
from app.text import (
    canonical_url,
    has_concrete_detail,
    normalize_space,
    title_fingerprint,
)
from app.virality import ViralityScore, score_virality

CATEGORY_TARGETS = {
    "security": 0.30,
    "ai": 0.25,
    "dev": 0.20,
    "bigtech": 0.15,
    "hardware": 0.05,
    "startups": 0.025,
    "tech": 0.025,
}


@dataclass(frozen=True)
class Candidate:
    item: StoryItem
    score: float
    virality: ViralityScore
    eligible: bool
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.item.title,
            "url": self.item.url,
            "source": self.item.source,
            "category": self.item.category,
            "score": round(self.score, 3),
            "virality": self.virality.to_dict(),
            "eligible": self.eligible,
            "reason": self.reason,
        }


def _recency_score(published_at: datetime, now: datetime) -> float:
    age = now - published_at
    if age <= timedelta(hours=6):
        return 0.6
    if age <= timedelta(hours=24):
        return 0.45
    if age <= timedelta(hours=48):
        return 0.25
    if age <= timedelta(days=4):
        return 0.05
    return -0.5


def _category_deficit(history: dict, category: str) -> float:
    recent = recent_categories(history, 10)
    target = CATEGORY_TARGETS.get(category, 0.02)
    if not recent:
        return target
    observed = sum(1 for value in recent if value == category) / len(recent)
    return target - observed


def _title_is_question(title: str) -> bool:
    return title.strip().endswith("?")


def score_item(item: StoryItem, history: dict, now: datetime) -> float:
    score_value = item.score
    score_value += _recency_score(item.published_at, now)
    score_value += _category_deficit(history, item.category) * 0.7

    recent = recent_categories(history, 10)
    if recent and recent[0] == item.category:
        score_value -= 0.12
    if recent[:2] == [item.category, item.category]:
        score_value -= 0.12
    if item.source in recent_sources(history, 8):
        score_value -= 0.06

    if has_concrete_detail(f"{item.title} {item.summary}"):
        score_value += 0.12
    if item.extra.get("cisa"):
        score_value += 0.45
    cvss = item.extra.get("cvss")
    if cvss is not None:
        try:
            score_value += max(0.0, min(float(cvss) / 20, 0.45))
        except (TypeError, ValueError):
            pass
    if item.extra.get("points"):
        try:
            score_value += min(math.log10(max(int(item.extra["points"]), 1)) / 12, 0.18)
        except (TypeError, ValueError):
            pass
    if item.source_kind == "devto":
        score_value -= 0.18
    if _title_is_question(item.title):
        score_value -= 0.35
    title_lower = item.title.lower()
    if re.search(
        r"\b(?:jailbroken|jailbreak|teardown|look inside|repairability|modded|benchmark)\b",
        title_lower,
    ):
        score_value -= 0.28
    if re.search(
        r"\b(?:privacy|gmail|account|accounts|scam|warning|breach|patch)\b",
        title_lower,
    ):
        score_value += 0.12
    if len(normalize_space(item.summary)) < 50:
        score_value -= 0.15
    return score_value


def _cluster_items(items: list[StoryItem]) -> list[list[StoryItem]]:
    clusters: list[list[StoryItem]] = []
    for item in items:
        item_key = title_fingerprint(item.title)
        placed = False
        for cluster in clusters:
            best = cluster[0]
            if canonical_url(best.url) == canonical_url(item.url):
                cluster.append(item)
                placed = True
                break
            if SequenceMatcher(None, title_fingerprint(best.title), item_key).ratio() >= 0.88:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    return clusters


def rank_candidates(
    items: list[StoryItem],
    history: dict,
    now: datetime,
    settings: Settings,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for item in items:
        reason = filter_item(item)
        if reason:
            candidates.append(
                Candidate(item, 0.0, score_virality(item), False, reason)
            )
            continue
        if was_seen(history, item):
            candidates.append(
                Candidate(item, 0.0, score_virality(item), False, "already seen")
            )
            continue
        if now - item.published_at > timedelta(days=4):
            candidates.append(
                Candidate(item, 0.0, score_virality(item), False, "older than 4 days")
            )
            continue

        virality = score_virality(item)
        if virality.score < settings.min_virality_score:
            candidates.append(
                Candidate(
                    item,
                    0.0,
                    virality,
                    False,
                    f"virality {virality.score} below {settings.min_virality_score}",
                )
            )
            continue

        combined = score_item(item, history, now) + (
            virality.score / 100 * settings.virality_weight
        )
        item.score = combined
        candidates.append(Candidate(item, combined, virality, True))

    candidates.sort(
        key=lambda candidate: (candidate.eligible, candidate.score, candidate.virality.score),
        reverse=True,
    )
    return candidates


def select_story(
    items: list[StoryItem], history: dict, now: datetime, settings: Settings
) -> Story | None:
    candidates = [candidate for candidate in rank_candidates(items, history, now, settings) if candidate.eligible]
    if not candidates:
        return None

    clusters = _cluster_items([candidate.item for candidate in candidates])
    by_url = {candidate.item.url: candidate for candidate in candidates}
    best_in_cluster = [
        max(
            (by_url[item.url] for item in cluster if item.url in by_url),
            key=lambda candidate: candidate.score,
        )
        for cluster in clusters
    ]
    best_in_cluster.sort(key=lambda candidate: candidate.score, reverse=True)
    top = best_in_cluster[0]
    if top.score < 0.55:
        return None
    return build_story(top.item)
