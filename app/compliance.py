from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.models import PostDraft, Story, StoryItem
from app.text import (
    canonical_url,
    count_hashtags,
    has_concrete_detail,
    normalize_space,
    split_sentences,
    strip_urls,
)

MAX_TEXT_LENGTH = 300
MIN_TEXT_LENGTH = 190
TARGET_MAX_LENGTH = 280

BLOCKED_TERMS = {
    "election",
    "trump",
    "biden",
    "harris",
    "modi",
    "congress",
    "senate",
    "parliament",
    "war",
    "gaza",
    "israel",
    "ukraine",
    "russia",
    "nato",
    "immigration",
    "abortion",
    "gun control",
    "murder",
    "shooting",
    "porn",
    "nsfw",
    "nude",
    "onlyfans",
    "racist",
    "slur",
    "bitcoin price",
    "crypto price",
    "ethereum price",
    "token price",
    "memecoin",
    "nft drop",
    "football",
    "cricket",
    "bollywood",
    "hollywood",
    "celebrity",
    "kardashian",
    "giveaway",
    "airdrop",
    "free followers",
    "stock price",
    "shares jump",
    "market cap",
}

NON_NEWS_PATTERNS = (
    r"\bsave up to\b",
    r"\bdeals?\b",
    r"\bcoupon\b",
    r"\bdiscount\b",
    r"\bpromo code\b",
    r"\bprime day\b",
    r"\bblack friday\b",
    r"\bgift guide\b",
    r"\bbuying guide\b",
    r"\bhow to\b",
    r"\btips? (?:to|for)\b",
    r"\bwhat to expect\b",
    r"\bthis week on\b",
    r"\broundup\b",
    r"\bweekly recap\b",
    r"\bapps to try\b",
    r"\bthings to try\b",
    r"\bthese .* apps\b",
    r"\brunning list\b",
    r"\bpart (?:ii|iii|iv|\d+)\b",
    r"\bhow [A-Z][\w&.]+ is\b",
    r"\bbrings? chatgpt\b",
    r"\brolls? out chatgpt\b",
    r"^what (?:are|is)\b",
    r"\bexplained\b",
    r"\bsecuring our\b",
    r"\breview\b",
    r"\bhands-on\b",
    r"\bopinion\b",
    r"^(?:i|we) (?:hope|think|wish)\b",
    r"\bbest .* deals?\b",
    r"\bprice drop\b",
    r"\bfor \$[0-9]",
    r"\bfrom (?:local|small) businesses to global brands\b",
    r"\bmore advertisers\b",
    r"\b(?:advertisers?|brands?)\b.*\b(?:have|has|can|could|will)\b.*\b(?:used|reach)\b",
    r"\b(?:ceo|cfo|coo|cto|chief .* officer|executive)\b.*\b(?:out|departs?|resigns?|steps? down|leaves?)\b",
    r"\b(?:is|are) out after\b",
    r"\bcould let you\b",
    r"\bbroad access through\b",
    r"\bconsistent standards\b",
    r"\bYouTuber\b",
    r"\b3D printer\b",
    r"\bproposed bringing back\b",
    r"\bset plan in motion\b",
    r"\bfinal gift\b",
    r"\bloyal fans\b",
    r"^\d+\s+best\b",
    r"\bbest\b.*\b(?:phones|laptops|tablets|headphones|tvs|monitors|gadgets)\b",
    r"\bunderusing\b",
    r"\bfeatures do you\b",
    r"\byou lose\b",
    r"^which\b",
    r"\bclosest equivalent\b",
    r"\bdebuted \d+ years ago\b",
    r"\byears ago today\b",
    r"\bcashing in on\b",
    r"^why\b",
    r"\bnow you can watch\b",
)

BLOCKED_SOURCE_DOMAINS = {
    "reddit.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "linkedin.com",
    "pinterest.com",
    "quora.com",
    "substack.com",
}

SHORTENER_DOMAINS = {
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "goo.gl",
    "ow.ly",
    "buff.ly",
    "is.gd",
    "cutt.ly",
    "shorturl.at",
}

ALLOWED_DOMAINS = {
    "php.net",
    "nvd.nist.gov",
    "cisa.gov",
    "msrc.microsoft.com",
    "aws.amazon.com",
    "blog.cloudflare.com",
    "cloudflare.com",
    "github.com",
    "github.blog",
    "gitlab.com",
    "openai.com",
    "deepmind.google",
    "huggingface.co",
    "anthropic.com",
    "thehackernews.com",
    "securityweek.com",
    "darkreading.com",
    "therecord.media",
    "krebsonsecurity.com",
    "arstechnica.com",
    "theverge.com",
    "techcrunch.com",
    "engadget.com",
    "wired.com",
    "bbc.co.uk",
    "bbc.com",
    "tomshardware.com",
    "go.dev",
    "python.org",
    "nodejs.org",
    "rust-lang.org",
    "nextjs.org",
    "react.dev",
    "news.ycombinator.com",
    "lobste.rs",
    "dev.to",
    "w3.org",
    "arxiv.org",
    "acm.org",
    "ieee.org",
    "mozilla.org",
    "kernel.org",
    "kubernetes.io",
    "docker.com",
    "nvidia.com",
    "amd.com",
    "intel.com",
    "apple.com",
    "samsung.com",
    "googleblog.com",
    "blog.google",
    "microsoft.com",
    "meta.com",
    "about.fb.com",
    "amazon.com",
}

BLOCKED_SUFFIXES = (".gov", ".edu")

CLICKBAIT_PATTERNS = [
    r"you won'?t believe",
    r"this changes everything",
    r"here'?s why",
    r"what happened next",
    r"shocking",
    r"mind[- ]blowing",
    r"must[- ]see",
    r"game[- ]?changer",
    r"no[- ]brainer",
    r"insane",
    r"unbelievable",
    r"\bhuge\b",
    r"\bmassive\b",
    r"\bcrazy\b",
    r"\bsecret\b",
    r"\bbreaking\b",
]

FIRST_PERSON_RE = re.compile(r"\bI\b|\b(?:we|our|my|me)\b", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![\w.])@[A-Za-z0-9_.]+")
QUESTION_RE = re.compile(r"\?")
SOURCE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class ComplianceError(RuntimeError):
    pass


def _host_matches(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def is_blocked_source(url: str) -> bool:
    host = urlparse(canonical_url(url)).netloc.removeprefix("www.")
    if not host:
        return True
    if _host_matches(host, BLOCKED_SOURCE_DOMAINS):
        return True
    if _host_matches(host, SHORTENER_DOMAINS):
        return True
    return False


def is_allowed_source(url: str) -> bool:
    host = urlparse(canonical_url(url)).netloc.removeprefix("www.")
    if not host:
        return False
    if is_blocked_source(url):
        return False
    if _host_matches(host, ALLOWED_DOMAINS):
        return True
    return host.endswith(BLOCKED_SUFFIXES)


def is_disallowed_text(value: str) -> str | None:
    lowered = (value or "").lower()
    for term in BLOCKED_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return term
    return None


def summary_quality(value: str) -> str | None:
    text = normalize_space(value)
    if not text:
        return "empty summary"
    if text.count('"') % 2 or text.count("“") != text.count("”"):
        return "unbalanced quote in summary"
    if text.endswith((",", ";", ":", " -", "—", "–")):
        return "truncated summary"
    if re.search(r"\b(?:and|or|including|like|with|to)$", text, re.IGNORECASE):
        return "truncated summary"
    return None


def filter_item(item: StoryItem) -> str | None:
    if not is_allowed_source(item.url):
        return "source domain is not allowlisted"
    blocked = is_disallowed_text(item.text_for_filtering)
    if blocked:
        return f"blocked term: {blocked}"
    quality = summary_quality(item.summary)
    if quality:
        return quality
    text = normalize_space(f"{item.title} {item.summary}")
    for pattern in NON_NEWS_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return "non-news content"
    if len(normalize_space(item.title)) < 18:
        return "title too short"
    if len(normalize_space(item.title)) > 180:
        return "title too long"
    return None


def validate_source_url(url: str, settings: Settings) -> tuple[bool, str]:
    if not is_allowed_source(url):
        return False, "source URL domain is not allowlisted"
    try:
        with httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=min(settings.request_timeout, 12),
            follow_redirects=True,
        ) as client:
            response = client.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    except httpx.HTTPError as exc:
        return False, f"source link request failed: {exc.__class__.__name__}"
    if response.status_code in {404, 410}:
        return False, f"source link returned HTTP {response.status_code}"
    final_url = str(response.url)
    if is_blocked_source(final_url):
        return False, "source URL redirected to a blocked domain"
    if not is_allowed_source(final_url):
        return False, "source URL redirected outside allowlist"
    return True, "ok"


def validate_text(draft: PostDraft, story: Story, settings: Settings) -> list[str]:
    errors: list[str] = []
    text = draft.text.strip()
    body = draft.body.strip()

    if not (MIN_TEXT_LENGTH <= len(text) <= MAX_TEXT_LENGTH):
        errors.append(f"text length {len(text)} outside {MIN_TEXT_LENGTH}-{MAX_TEXT_LENGTH}")
    if len(text) > TARGET_MAX_LENGTH + 20:
        errors.append("text is too long for the crisp target")

    if not draft.source_refs:
        errors.append("no source references")
    if len(draft.source_refs) > settings.source_refs_limit:
        errors.append("too many source references")

    source_names = [ref.name for ref in draft.source_refs]
    if not re.search(r"\bSources?\s*:", text, re.IGNORECASE):
        errors.append("missing source line")
    if not any(name.lower() in text.lower() for name in source_names):
        errors.append("source name is not visible in text")

    for ref in draft.source_refs:
        if not is_allowed_source(ref.url):
            errors.append(f"source domain not allowlisted: {ref.url}")

    body_lower = body.lower()
    if FIRST_PERSON_RE.search(body):
        errors.append("first-person language detected")
    if "?" in body:
        errors.append("question mark/rhetorical question detected")
    if MENTION_RE.search(body):
        errors.append("mention detected")
    if count_hashtags(body) > 2:
        errors.append("too many hashtags")

    for pattern in CLICKBAIT_PATTERNS:
        if re.search(pattern, body_lower):
            errors.append(f"clickbait/hype phrase detected: {pattern}")

    blocked = is_disallowed_text(body)
    if blocked:
        errors.append(f"blocked term: {blocked}")

    sentences = split_sentences(body)
    if not (3 <= len(sentences) <= 5):
        errors.append(f"body has {len(sentences)} sentences; expected 3-5")
    if body and not has_concrete_detail(body):
        errors.append("no concrete number/version/date in body")
    if body and not sentences:
        errors.append("empty body")
    if sentences and re.match(
        r"^(?:in a major development|the company announced|big news|huge news)\b",
        sentences[0],
        re.IGNORECASE,
    ):
        errors.append("weak or press-release-like opening")

    urls = SOURCE_URL_RE.findall(body)
    allowed_urls = {canonical_url(ref.url) for ref in draft.source_refs}
    for url in urls:
        if canonical_url(url) not in allowed_urls:
            errors.append(f"unexpected URL in body: {url}")

    if len(draft.image_title.split()) > 8:
        errors.append("image title has more than 8 words")
    if not (1 <= len(draft.image_facts) <= 3):
        errors.append("image needs 1-3 facts")
    story_text = normalize_space(
        f"{story.title} {story.summary} " + " ".join(str(v) for v in story.facts.values())
    ).lower()
    body_text = normalize_space(body).lower()
    for fact in draft.image_facts:
        fact_norm = normalize_space(fact)
        if len(fact_norm) < 3 or len(fact_norm) > 60:
            errors.append(f"image fact length invalid: {fact_norm}")
            continue
        searchable = f"{story_text} {body_text}"
        number_tokens = re.findall(
            r"\b(?:CVE-\d{4}-\d{4,}|\d{4}-\d{2}-\d{2}|\d+(?:\.\d+)*%?)\b",
            fact_norm,
            re.IGNORECASE,
        )
        if number_tokens:
            if any(token.lower() not in searchable for token in number_tokens):
                errors.append(f"image fact not supported by source: {fact_norm}")
        else:
            tokens = [
                token
                for token in re.findall(r"[a-z0-9]+", fact_norm.lower())
                if len(token) > 2
            ]
            if tokens:
                overlap = sum(1 for token in tokens if token in searchable)
                if overlap / len(tokens) < 0.6:
                    errors.append(f"image fact weakly supported: {fact_norm}")
    if not (10 <= len(draft.alt_text) <= 300):
        errors.append("ALT text length invalid")

    return errors


def validate_image(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return ["image file missing"]
    try:
        from PIL import Image
    except ImportError:
        return ["Pillow is not installed"]
    try:
        with Image.open(path) as image:
            if image.size != (1200, 675):
                errors.append(f"image dimensions {image.size}, expected (1200, 675)")
            if image.format not in {"JPEG", "PNG"}:
                errors.append(f"image format {image.format}, expected JPEG or PNG")
    except OSError as exc:
        errors.append(f"image cannot be opened: {exc}")
    if path.stat().st_size >= 2_000_000:
        errors.append("image is 2MB or larger")
    return errors


def _post_datetime(post: dict, settings: Settings) -> datetime | None:
    try:
        posted_at = datetime.fromisoformat(post.get("posted_at", ""))
    except (TypeError, ValueError):
        return None
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=ZoneInfo(settings.timezone))
    return posted_at


def local_posts_today(state: dict, now: datetime, settings: Settings) -> list[dict]:
    tz = ZoneInfo(settings.timezone)
    today = now.astimezone(tz).date().isoformat()
    result = []
    for post in state.get("posts", []):
        posted_at = _post_datetime(post, settings)
        if posted_at and posted_at.astimezone(tz).date().isoformat() == today:
            result.append(post)
    return result


def breaking_posts_since(
    state: dict, now: datetime, settings: Settings, days: int = 7
) -> int:
    cutoff = now - timedelta(days=days)
    count = 0
    for post in state.get("posts", []):
        if not post.get("breaking"):
            continue
        posted_at = _post_datetime(post, settings)
        if posted_at and posted_at >= cutoff:
            count += 1
    return count


def post_window_reason(now: datetime, settings: Settings) -> str:
    timezone = ZoneInfo(settings.timezone)
    local = now.astimezone(timezone)
    windows = ((time(8, 0), time(9, 30)), (time(19, 0), time(20, 30)))
    for start, end in windows:
        if start <= local.time() <= end:
            return "inside posting window"
    return f"outside posting windows ({local.strftime('%H:%M')} {settings.timezone})"


def can_post_now(
    state: dict,
    now: datetime,
    is_breaking: bool,
    settings: Settings,
    *,
    ignore_window: bool = False,
) -> tuple[bool, str]:
    today_posts = local_posts_today(state, now, settings)
    if len(today_posts) >= settings.max_total_posts_per_day:
        return False, "daily hard cap reached"

    regular_today = sum(1 for post in today_posts if not post.get("breaking"))
    if regular_today >= settings.max_posts_per_day and not is_breaking:
        return False, "regular daily cap reached"

    if is_breaking:
        breaking_today = sum(1 for post in today_posts if post.get("breaking"))
        if breaking_today >= settings.max_breaking_posts_per_day:
            return False, "breaking daily cap reached"
        if breaking_posts_since(state, now, settings) >= settings.max_breaking_posts_per_week:
            return False, "breaking weekly cap reached"

    if ignore_window:
        return True, "scheduler override"
    reason = post_window_reason(now, settings)
    if "inside" in reason:
        return True, reason
    if is_breaking:
        return True, "breaking-news override"
    return False, reason
