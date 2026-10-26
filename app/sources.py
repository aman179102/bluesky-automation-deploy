from __future__ import annotations

import concurrent.futures
import json
import math
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

import httpx

from app.config import Settings
from app.models import SourceRef, Story, StoryItem
from app.text import (
    canonical_url,
    clean_markdown,
    domain,
    normalize_space,
    source_display_name,
    strip_html,
    title_fingerprint,
    truncate_words,
)

NOW = lambda: datetime.now(timezone.utc)


@dataclass(frozen=True)
class FeedSpec:
    name: str
    url: str
    category: str
    weight: float
    limit: int = 12


FEEDS: tuple[FeedSpec, ...] = (
    FeedSpec("PHP.net", "https://www.php.net/feed.atom", "dev", 1.0, 10),
    FeedSpec("The Hacker News", "https://feeds.feedburner.com/TheHackersNews", "security", 0.95),
    FeedSpec("SecurityWeek", "https://www.securityweek.com/feed/", "security", 0.9),
    FeedSpec("Dark Reading", "https://www.darkreading.com/rss.xml", "security", 0.9),
    FeedSpec("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index", "tech", 0.85),
    FeedSpec("The Verge", "https://www.theverge.com/rss/index.xml", "bigtech", 0.8),
    FeedSpec("TechCrunch", "https://techcrunch.com/feed/", "startups", 0.8),
    FeedSpec("Engadget", "https://www.engadget.com/rss.xml", "tech", 0.75),
    FeedSpec("Wired", "https://www.wired.com/feed/rss", "tech", 0.8),
    FeedSpec("BBC Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml", "tech", 0.8),
    FeedSpec("Tom's Hardware", "https://www.tomshardware.com/feeds/all", "hardware", 0.8),
    FeedSpec("OpenAI", "https://openai.com/news/rss.xml", "ai", 0.95),
    FeedSpec("Google DeepMind", "https://deepmind.google/blog/rss.xml", "ai", 0.95),
    FeedSpec("Hugging Face", "https://huggingface.co/blog/feed.xml", "ai", 0.9),
    FeedSpec("GitHub Blog", "https://github.blog/feed/", "dev", 0.9),
    FeedSpec("Cloudflare", "https://blog.cloudflare.com/rss/", "security", 0.9),
    FeedSpec("Microsoft Security", "https://www.microsoft.com/security/blog/feed/", "security", 0.95),
    FeedSpec("AWS Security", "https://aws.amazon.com/security/security-bulletins/rss/feed/", "security", 0.95),
    FeedSpec("Go Blog", "https://go.dev/blog/feed.atom", "dev", 0.85),
    FeedSpec("Python Insider", "https://pythoninsider.blogspot.com/feeds/posts/default", "dev", 0.85),
    FeedSpec("Node.js", "https://nodejs.org/en/feed/blog.xml", "dev", 0.85),
    FeedSpec("Rust Blog", "https://blog.rust-lang.org/feed.xml", "dev", 0.85),
    FeedSpec("Next.js", "https://nextjs.org/feed.xml", "dev", 0.8),
    FeedSpec("React", "https://react.dev/rss.xml", "dev", 0.8),
)

GITHUB_REPOS = (
    "python/cpython",
    "golang/go",
    "nodejs/node",
    "rust-lang/rust",
    "vercel/next.js",
    "facebook/react",
)

SECURITY_WORDS = (
    "cve",
    "vulnerability",
    "vulnerabilities",
    "exploit",
    "ransomware",
    "malware",
    "security",
    "breach",
    "zero-day",
    "advisory",
    "remote code execution",
    "rce",
    "xss",
    "sql injection",
    "phishing",
    "botnet",
    "cisa",
    "nvd",
    "patch",
)

AI_WORDS = (
    "openai",
    "anthropic",
    "deepmind",
    "gemini",
    "chatgpt",
    "claude",
    "llm",
    "large language model",
    "artificial intelligence",
    "machine learning",
    "hugging face",
    "diffusion",
    "transformer",
    "ai model",
    "ai agent",
    "inference",
)

DEV_WORDS = (
    "release",
    "released",
    "version",
    "compiler",
    "runtime",
    "python",
    "rust",
    "golang",
    "node.js",
    "react",
    "next.js",
    "kubernetes",
    "docker",
    "github",
    "open source",
    "open-source",
    "sdk",
    "framework",
    "database",
    "linux",
    "kernel",
    "developer",
    "api",
)

HARDWARE_WORDS = (
    "chip",
    "gpu",
    "cpu",
    "semiconductor",
    "iphone",
    "pixel",
    "samsung",
    "laptop",
    "processor",
    "nvidia",
    "amd",
    "intel",
    "tsmc",
    "hardware",
    "device",
    "tablet",
    "wearable",
)

STARTUP_WORDS = (
    "raises",
    "funding",
    "series a",
    "series b",
    "series c",
    "valuation",
    "startup",
    "acquires",
    "acquisition",
    "ipo",
)


def parse_date(value: str | None, fallback: datetime | None = None) -> datetime:
    fallback = fallback or NOW()
    if not value:
        return fallback
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return fallback


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ET.Element, names: Iterable[str]) -> str:
    wanted = set(names)
    for child in element:
        if _local_name(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _child_link(element: ET.Element) -> str:
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        text = (child.text or "").strip()
        if href and (not text or child.attrib.get("rel") in {None, "alternate"}):
            return href
        if text:
            return text
    return ""


def classify_category(title: str, summary: str, default: str = "tech") -> str:
    text = f"{title} {summary}".lower()
    if any(word in text for word in SECURITY_WORDS):
        return "security"
    if any(word in text for word in AI_WORDS):
        return "ai"
    if any(word in text for word in DEV_WORDS):
        return "dev"
    if re.search(
        r"\b(?:google|android|gmail|microsoft|apple|meta|amazon|openai|xbox|windows)\b",
        text,
    ) and re.search(
        r"\b(?:account|accounts|privacy|policy|gmail|advertising|ads|settings|data)\b",
        text,
    ):
        return "bigtech"
    if any(word in text for word in HARDWARE_WORDS):
        return "hardware"
    if any(word in text for word in STARTUP_WORDS):
        return "startups"
    if default in {"security", "ai", "dev", "hardware", "startups", "bigtech", "tech"}:
        return default
    return "tech"


def parse_feed(
    payload: bytes,
    spec: FeedSpec,
    *,
    now: datetime | None = None,
) -> list[StoryItem]:
    now = now or NOW()
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    entries = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    items: list[StoryItem] = []
    for entry in entries[: spec.limit * 2]:
        title = normalize_space(_child_text(entry, {"title"}))
        link = _child_link(entry)
        if not title or not link:
            continue
        summary = strip_html(
            _child_text(entry, {"description", "summary", "content", "encoded"})
        )
        published = parse_date(
            _child_text(entry, {"pubdate", "published", "updated", "date"}), now
        )
        if published > now + timedelta(hours=6):
            published = now
        category = classify_category(title, summary, spec.category)
        items.append(
            StoryItem(
                source=spec.name,
                title=title,
                url=canonical_url(link),
                summary=summary,
                published_at=published,
                category=category,
                guid=_child_text(entry, {"guid", "id"}),
                score=spec.weight,
                source_kind="rss",
                extra={"feed": spec.name},
            )
        )
        if len(items) >= spec.limit:
            break
    return items


def fetch_feed(
    client: httpx.Client, spec: FeedSpec, settings: Settings
) -> tuple[list[StoryItem], str | None]:
    try:
        response = client.get(
            spec.url,
            headers={"Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml,*/*"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return [], f"{spec.name}: {exc.__class__.__name__}"
    items = parse_feed(response.content, spec)
    return items, None if items else f"{spec.name}: no items"


def fetch_hn(client: httpx.Client, settings: Settings) -> list[StoryItem]:
    response = client.get(
        "https://hn.algolia.com/api/v1/search",
        params={"tags": "front_page", "hitsPerPage": 50},
    )
    response.raise_for_status()
    payload = response.json()
    items: list[StoryItem] = []
    now = NOW()
    for hit in payload.get("hits", []):
        title = normalize_space(hit.get("title") or "")
        if not title:
            continue
        points = int(hit.get("points") or 0)
        if points < 60:
            continue
        created = datetime.fromtimestamp(
            int(hit.get("created_at_i") or now.timestamp()), tz=timezone.utc
        )
        if now - created > timedelta(hours=48):
            continue
        external = canonical_url(hit.get("url") or "")
        discussion = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        if external and _source_allowed_for_fetch(external):
            url = external
            source = source_display_name(external, "Hacker News")
        else:
            url = discussion
            source = "Hacker News"
        summary = strip_html(hit.get("story_text") or "")
        if not summary:
            summary = f"{points} points and {hit.get('num_comments') or 0} comments on Hacker News."
        category = classify_category(title, summary, "tech")
        items.append(
            StoryItem(
                source=source,
                title=title,
                url=url,
                summary=summary,
                published_at=created,
                category=category,
                guid=str(hit.get("objectID") or ""),
                score=0.7 + min(math.log10(max(points, 1)) / 10, 0.3),
                source_kind="hn",
                extra={
                    "points": points,
                    "comments": hit.get("num_comments") or 0,
                    "discussion_url": discussion,
                    "via": "Hacker News",
                },
            )
        )
    return items


def fetch_lobsters(client: httpx.Client, settings: Settings) -> list[StoryItem]:
    response = client.get("https://lobste.rs/hottest.json")
    response.raise_for_status()
    payload = response.json()
    now = NOW()
    items: list[StoryItem] = []
    for story in payload:
        title = normalize_space(story.get("title") or "")
        if not title:
            continue
        score_value = int(story.get("score") or 0)
        if score_value < 10:
            continue
        created = parse_date(story.get("created_at"), now)
        if now - created > timedelta(hours=48):
            continue
        discussion = story.get("comments_url") or story.get("short_id_url") or ""
        external = canonical_url(story.get("url") or "")
        if external and _source_allowed_for_fetch(external):
            url = external
            source = source_display_name(external, "Lobsters")
        else:
            url = canonical_url(discussion)
            source = "Lobsters"
        summary = strip_html(story.get("description") or "")
        if not summary:
            summary = f"{score_value} points on Lobsters."
        category = classify_category(title, summary, "dev")
        items.append(
            StoryItem(
                source=source,
                title=title,
                url=url,
                summary=summary,
                published_at=created,
                category=category,
                guid=str(story.get("short_id") or ""),
                score=0.62 + min(math.log10(max(score_value, 1)) / 12, 0.2),
                source_kind="lobsters",
                extra={
                    "points": score_value,
                    "discussion_url": discussion,
                    "via": "Lobsters",
                },
            )
        )
    return items


def fetch_devto(client: httpx.Client, settings: Settings) -> list[StoryItem]:
    response = client.get(
        "https://dev.to/api/articles",
        params={"per_page": 30, "top": 7},
    )
    response.raise_for_status()
    payload = response.json()
    now = NOW()
    news_words = re.compile(
        r"\b(release|released|launch|security|vulnerability|open source|update|version|ai|model|breaking)\b",
        re.I,
    )
    items: list[StoryItem] = []
    for article in payload:
        title = normalize_space(article.get("title") or "")
        if not title or not news_words.search(title):
            continue
        published = parse_date(article.get("published_at"), now)
        if now - published > timedelta(days=3):
            continue
        reactions = int(article.get("positive_reactions_count") or 0)
        summary = strip_html(article.get("description") or "")
        category = classify_category(title, summary, "dev")
        items.append(
            StoryItem(
                source="DEV Community",
                title=title,
                url=canonical_url(article.get("url") or ""),
                summary=summary,
                published_at=published,
                category=category,
                guid=str(article.get("id") or ""),
                score=0.45 + min(math.log10(max(reactions, 1)) / 20, 0.1),
                source_kind="devto",
                extra={"reactions": reactions, "tags": article.get("tag_list") or []},
            )
        )
    return items


def parse_kev_payload(payload: dict[str, Any], now: datetime | None = None) -> list[StoryItem]:
    now = now or NOW()
    items: list[StoryItem] = []
    for entry in payload.get("vulnerabilities", []):
        cve = normalize_space(entry.get("cveID") or "")
        if not re.match(r"CVE-\d{4}-\d{4,}", cve):
            continue
        date_added = parse_date(entry.get("dateAdded"), now)
        if now - date_added > timedelta(days=21):
            continue
        vendor = normalize_space(entry.get("vendorProject") or "")
        product = normalize_space(entry.get("product") or "")
        name = normalize_space(entry.get("vulnerabilityName") or cve)
        description = normalize_space(entry.get("shortDescription") or "")
        required_action = normalize_space(entry.get("requiredAction") or "")
        due_date = normalize_space(entry.get("dueDate") or "")
        ransomware = normalize_space(entry.get("knownRansomwareCampaignUse") or "Unknown")
        title = f"{vendor} {product}: {cve} added to CISA exploited list"
        summary = description
        if required_action:
            summary += f" Required action: {required_action}"
        category = "security"
        items.append(
            StoryItem(
                source="NVD",
                title=normalize_space(title),
                url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                summary=normalize_space(summary),
                published_at=date_added,
                category=category,
                guid=cve,
                score=1.4,
                source_kind="kev",
                extra={
                    "cisa": True,
                    "cve": cve,
                    "vendor": vendor,
                    "product": product,
                    "vulnerability_name": name,
                    "required_action": required_action,
                    "due_date": due_date,
                    "ransomware": ransomware,
                    "cisa_url": "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
                },
            )
        )
    items.sort(key=lambda item: item.published_at, reverse=True)
    return items


def fetch_cisa_kev(client: httpx.Client, settings: Settings) -> list[StoryItem]:
    response = client.get(
        "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
    )
    response.raise_for_status()
    return parse_kev_payload(response.json())[:6]


def fetch_github_releases(client: httpx.Client, settings: Settings) -> list[StoryItem]:
    now = NOW()
    headers = {"Accept": "application/vnd.github+json"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    items: list[StoryItem] = []
    for repo in GITHUB_REPOS:
        try:
            response = client.get(
                f"https://api.github.com/repos/{repo}/releases",
                params={"per_page": 5},
                headers=headers,
            )
            if response.status_code in {403, 429}:
                continue
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        for release in response.json():
            if release.get("draft") or release.get("prerelease"):
                continue
            published = parse_date(release.get("published_at"), now)
            if now - published > timedelta(days=4):
                continue
            tag = normalize_space(release.get("tag_name") or "")
            name = normalize_space(release.get("name") or "")
            title = f"{repo.split('/')[-1]} {tag or name} released"
            body = clean_markdown(release.get("body") or "")
            summary = body[:500] if body else (name or title)
            category = classify_category(title, summary, "dev")
            items.append(
                StoryItem(
                    source="GitHub",
                    title=normalize_space(title),
                    url=canonical_url(release.get("html_url") or ""),
                    summary=normalize_space(summary),
                    published_at=published,
                    category=category,
                    guid=str(release.get("id") or ""),
                    score=0.75,
                    source_kind="github",
                    extra={"repo": repo, "tag": tag, "release_name": name},
                )
            )
    return items


def _source_allowed_for_fetch(url: str) -> bool:
    from app.compliance import is_allowed_source

    return is_allowed_source(url)


class _ParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._current: list[str] = []
        self._in_paragraph = False
        self._skip_depth = 0
        self._skipped = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form"}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._skipped:
            self._skip_depth += 1
        elif tag == "p" and not self._skip_depth:
            self._in_paragraph = True
            self._current = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._skipped and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "p" and self._in_paragraph:
            text = normalize_space(" ".join(self._current))
            if len(text) >= 60:
                self.paragraphs.append(text)
            self._in_paragraph = False
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_paragraph and not self._skip_depth:
            text = normalize_space(data)
            if text:
                self._current.append(text)


def extract_article_text(html: str, limit: int = 4000) -> str:
    parser = _ParagraphParser()
    try:
        parser.feed(html)
    except Exception:
        return ""
    paragraphs: list[str] = []
    seen: set[str] = set()
    for paragraph in parser.paragraphs:
        key = paragraph.lower()
        if key in seen:
            continue
        seen.add(key)
        paragraphs.append(paragraph)
        if sum(len(value) for value in paragraphs) >= limit:
            break
    return truncate_words(" ".join(paragraphs), limit)


def _fetch_article_text(
    story: Story, client: httpx.Client, settings: Settings
) -> str:
    if story.facts.get("cisa"):
        return ""
    try:
        response = client.get(
            story.primary_source.url,
            headers={"User-Agent": settings.user_agent, "Accept": "text/html"},
        )
    except httpx.HTTPError:
        return ""
    if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
        return ""
    return extract_article_text(response.text)


def enrich_story(story: Story, client: httpx.Client, settings: Settings) -> Story:
    cve = story.item.extra.get("cve")
    if cve:
        headers = {"User-Agent": settings.user_agent}
        if settings.nvd_api_key:
            headers["apiKey"] = settings.nvd_api_key
        try:
            response = client.get(
                "https://services.nvd.nist.gov/rest/json/cves/2.0",
                params={"cveId": cve},
                headers=headers,
            )
            if response.status_code == 200:
                vulnerabilities = response.json().get("vulnerabilities") or []
                if vulnerabilities:
                    cve_data = vulnerabilities[0].get("cve", {})
                    metrics = cve_data.get("metrics", {})
                    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
                        values = metrics.get(key) or []
                        if values:
                            data = values[0].get("cvssData") or {}
                            score = data.get("baseScore")
                            severity = data.get("baseSeverity")
                            if score is not None:
                                story.facts["cvss"] = score
                                story.facts["severity"] = severity
                                story.item.extra["cvss"] = score
                                story.item.extra["severity"] = severity
                                break
                    descriptions = cve_data.get("descriptions") or []
                    for description in descriptions:
                        if description.get("lang") == "en" and description.get("value"):
                            story.facts["nvd_description"] = description["value"]
                            break
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass

    article_text = _fetch_article_text(story, client, settings)
    if article_text:
        story.facts["article_text"] = truncate_words(article_text, 1200)
    return story


def build_story(item: StoryItem) -> Story:
    refs = [SourceRef(item.source, item.url, "primary")]
    cisa_url = item.extra.get("cisa_url")
    if cisa_url:
        refs.append(SourceRef("CISA KEV", cisa_url, "secondary"))
    discussion = item.extra.get("discussion_url")
    if discussion and canonical_url(discussion) != canonical_url(item.url):
        via = item.extra.get("via") or "Discussion"
        refs.append(SourceRef(via, discussion, "secondary"))
    facts = {
        "title": item.title,
        "summary": item.summary,
        "source": item.source,
        "published_at": item.published_at.isoformat(),
        "category": item.category,
        **item.extra,
    }
    return Story(item=item, sources=refs[:2], facts=facts)


def _dedupe(items: list[StoryItem]) -> list[StoryItem]:
    by_url: dict[str, StoryItem] = {}
    by_title: dict[str, StoryItem] = {}
    for item in items:
        if not item.url:
            continue
        url_key = canonical_url(item.url)
        title_key = title_fingerprint(item.title)
        existing = by_url.get(url_key) or by_title.get(title_key)
        if existing:
            if item.score > existing.score:
                by_url[url_key] = item
                by_title[title_key] = item
            continue
        by_url[url_key] = item
        by_title[title_key] = item
    return list({canonical_url(item.url): item for item in by_url.values()}.values())


def fetch_all(settings: Settings) -> tuple[list[StoryItem], list[str]]:
    items: list[StoryItem] = []
    errors: list[str] = []
    with httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=settings.request_timeout,
        follow_redirects=True,
    ) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_feed, client, spec, settings): spec for spec in FEEDS
            }
            for future in concurrent.futures.as_completed(futures):
                spec = futures[future]
                try:
                    feed_items, error = future.result()
                except Exception as exc:  # defensive: one feed must not kill the run
                    errors.append(f"{spec.name}: {exc.__class__.__name__}")
                    continue
                items.extend(feed_items)
                if error:
                    errors.append(error)

        for name, fetcher in (
            ("Hacker News", fetch_hn),
            ("Lobsters", fetch_lobsters),
            ("DEV Community", fetch_devto),
            ("CISA KEV", fetch_cisa_kev),
            ("GitHub releases", fetch_github_releases),
        ):
            try:
                items.extend(fetcher(client, settings))
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                errors.append(f"{name}: {exc.__class__.__name__}")

    from app.compliance import filter_item

    filtered: list[StoryItem] = []
    for item in items:
        reason = filter_item(item)
        if reason:
            continue
        filtered.append(item)
    filtered = _dedupe(filtered)
    filtered.sort(key=lambda item: item.published_at, reverse=True)
    return filtered[: settings.max_source_items], errors
