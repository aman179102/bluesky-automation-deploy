from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.text import short_url


@dataclass(frozen=True)
class SourceRef:
    name: str
    url: str
    kind: str = "primary"

    @property
    def short(self) -> str:
        return short_url(self.url)


@dataclass
class StoryItem:
    source: str
    title: str
    url: str
    summary: str
    published_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    category: str = "tech"
    guid: str = ""
    score: float = 0.0
    source_kind: str = "rss"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text_for_filtering(self) -> str:
        return f"{self.title} {self.summary}"

    @property
    def is_breaking(self) -> bool:
        if self.extra.get("cisa"):
            return True
        cvss = self.extra.get("cvss")
        return bool(cvss is not None and float(cvss) >= 9.0)


@dataclass
class Story:
    item: StoryItem
    sources: list[SourceRef]
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.item.title

    @property
    def summary(self) -> str:
        return self.item.summary

    @property
    def category(self) -> str:
        return self.item.category

    @property
    def is_breaking(self) -> bool:
        return self.item.is_breaking

    @property
    def primary_source(self) -> SourceRef:
        return self.sources[0]


@dataclass
class PostDraft:
    text: str
    body: str
    source_refs: list[SourceRef]
    image_title: str
    image_facts: list[str]
    alt_text: str
    category: str
    hashtags: list[str] = field(default_factory=list)
    provider: str = "template"

    @property
    def source_name(self) -> str:
        return self.source_refs[0].name if self.source_refs else ""
