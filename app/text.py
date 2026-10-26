from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
    "cmpid",
    "ocid",
    "smid",
}

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002600-\U000026FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+"
)
HASHTAG_RE = re.compile(r"(?<!\w)#[A-Za-z0-9_]+")
SOURCE_LINE_RE = re.compile(r"^\s*(?:sources?|via|credit)\s*:", re.IGNORECASE)

DOMAIN_NAMES = {
    "php.net": "PHP.net",
    "nvd.nist.gov": "NVD",
    "cisa.gov": "CISA",
    "msrc.microsoft.com": "Microsoft MSRC",
    "aws.amazon.com": "AWS Security",
    "blog.cloudflare.com": "Cloudflare",
    "github.com": "GitHub",
    "github.blog": "GitHub",
    "openai.com": "OpenAI",
    "deepmind.google": "Google DeepMind",
    "huggingface.co": "Hugging Face",
    "anthropic.com": "Anthropic",
    "thehackernews.com": "The Hacker News",
    "securityweek.com": "SecurityWeek",
    "darkreading.com": "Dark Reading",
    "therecord.media": "The Record",
    "krebsonsecurity.com": "Krebs on Security",
    "arstechnica.com": "Ars Technica",
    "theverge.com": "The Verge",
    "techcrunch.com": "TechCrunch",
    "engadget.com": "Engadget",
    "wired.com": "Wired",
    "bbc.co.uk": "BBC",
    "bbc.com": "BBC",
    "tomshardware.com": "Tom's Hardware",
    "go.dev": "Go Blog",
    "python.org": "Python",
    "nodejs.org": "Node.js",
    "rust-lang.org": "Rust Blog",
    "nextjs.org": "Next.js",
    "react.dev": "React",
    "news.ycombinator.com": "Hacker News",
    "lobste.rs": "Lobsters",
    "dev.to": "DEV Community",
}

CATEGORY_HASHTAGS = {
    "security": "#Security",
    "ai": "#AI",
    "dev": "#Dev",
    "hardware": "#Hardware",
    "startups": "#Startups",
    "bigtech": "#Tech",
    "tech": "#Tech",
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?is)<br\s*/?>", " ", value)
    value = re.sub(r"(?is)</p\s*>", " ", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    return normalize_space(html.unescape(value))


def clean_markdown(value: str) -> str:
    value = value or ""
    value = re.sub(r"```.*?```", " ", value, flags=re.S)
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"^#{1,6}\s*", "", value, flags=re.M)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(html.unescape(value))


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse(("https", host, path, "", urlencode(query), ""))


def short_url(url: str, max_len: int = 34) -> str:
    parsed = urlparse(canonical_url(url))
    if not parsed.netloc:
        return ""
    host = parsed.netloc.removeprefix("www.")
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    candidate = f"{host}{path}{query}"
    if len(candidate) <= max_len:
        return candidate
    if path and len(host + path) <= max_len:
        return host + path
    return host


def domain(url: str) -> str:
    return urlparse(canonical_url(url)).netloc.removeprefix("www.")


def source_display_name(url: str, fallback: str = "") -> str:
    host = domain(url)
    if host in DOMAIN_NAMES:
        return DOMAIN_NAMES[host]
    for known, name in DOMAIN_NAMES.items():
        if host.endswith("." + known):
            return name
    return fallback or host or "Source"


def title_fingerprint(title: str) -> str:
    stopwords = {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "new",
        "now",
        "how",
        "why",
        "what",
    }
    tokens = re.findall(r"[a-z0-9]+", (title or "").lower())
    keep = [token for token in tokens if token not in stopwords and (len(token) > 2 or token.isdigit())]
    return " ".join(keep) or normalize_space((title or "").lower())


def split_sentences(value: str) -> list[str]:
    value = normalize_space(value)
    if not value:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", value)
    return [part.strip() for part in parts if part.strip()]


def truncate_words(value: str, max_chars: int, ellipsis: str = "…") -> str:
    value = normalize_space(value)
    if len(value) <= max_chars:
        return value
    cut = value[: max(0, max_chars - len(ellipsis))]
    cut = cut.rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut + ellipsis) if cut else value[:max_chars]


def strip_urls(value: str) -> str:
    return normalize_space(URL_RE.sub(" ", value))


def strip_hashtags(value: str) -> str:
    return normalize_space(HASHTAG_RE.sub(" ", value))


def strip_emojis(value: str) -> str:
    return EMOJI_RE.sub("", value)


def clean_body(value: str) -> str:
    text = clean_markdown(value)
    text = strip_emojis(text)
    text = strip_urls(text)
    text = strip_hashtags(text)
    text = re.sub(
        r"(?is)(?:^|\n|(?<=[.!?])\s+)(?:sources?|via|credit)\s*:.*$",
        "",
        text,
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ensure_period(value: str) -> str:
    value = normalize_space(value)
    if value and value[-1] not in ".!?":
        value += "."
    return value


def hashtag_for_category(category: str) -> str:
    return CATEGORY_HASHTAGS.get(category, "#Tech")


def count_hashtags(value: str) -> int:
    return len(HASHTAG_RE.findall(value or ""))


def has_concrete_detail(value: str) -> bool:
    return bool(
        re.search(r"\bCVE-\d{4}-\d{4,}\b", value, re.I)
        or re.search(r"\b\d+(?:\.\d+)+\b", value)
        or re.search(r"\b\d+(?:\.\d+)?%", value)
        or re.search(r"\b\d{1,3}(?:,\d{3})+\b", value)
        or re.search(r"\b\d+\s+(?:million|billion|trillion)\b", value, re.I)
        or re.search(r"[$€£₹]\s?\d", value)
        or re.search(r"\b(?:19|20)\d{2}\b", value)
    )


def sentence_case(value: str) -> str:
    value = normalize_space(value)
    if not value:
        return value
    return value[0].upper() + value[1:]


def source_line(refs: list, include_short: bool = True) -> str:
    if not refs:
        return ""
    if len(refs) == 1:
        ref = refs[0]
        line = f"Source: {ref.name}"
        if include_short:
            short = ref.short
            if short and short.lower() not in ref.name.lower() and len(short) <= 30:
                line += f" — {short}"
        return line
    names = ", ".join(ref.name for ref in refs[:2])
    return f"Sources: {names}"
