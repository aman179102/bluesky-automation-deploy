from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import StoryItem
from app.text import normalize_space

MASS_BRANDS: dict[str, int] = {
    "openai": 30,
    "chatgpt": 30,
    "google": 28,
    "gemini": 24,
    "apple": 30,
    "iphone": 30,
    "ios": 24,
    "microsoft": 28,
    "windows": 28,
    "android": 25,
    "chrome": 25,
    "meta": 25,
    "instagram": 25,
    "whatsapp": 30,
    "youtube": 25,
    "amazon": 25,
    "nvidia": 28,
    "tesla": 25,
    "tiktok": 30,
    "netflix": 20,
    "spotify": 20,
    "github": 20,
    "linux": 20,
    "python": 18,
    "wordpress": 20,
    "cloudflare": 18,
    "deepseek": 25,
    "anthropic": 22,
    "claude": 22,
    "perplexity": 20,
    "grok": 22,
    "firefox": 18,
    "safari": 22,
    "zoom": 18,
    "slack": 16,
    "notion": 16,
    "figma": 16,
    "canva": 16,
    "fortnite": 20,
    "minecraft": 20,
    "roblox": 20,
    "steam": 18,
    "playstation": 20,
    "xbox": 20,
    "uber": 18,
    "airbnb": 18,
    "stripe": 16,
    "shopify": 18,
    "vercel": 12,
    "hugging face": 15,
    "spacex": 22,
    "starlink": 20,
}

BROAD_IMPACT: dict[str, int] = {
    "free": 12,
    "price": 10,
    "pricing": 10,
    "subscription": 8,
    "privacy": 12,
    "data breach": 14,
    "breach": 10,
    "outage": 12,
    "goes down": 10,
    "ban": 10,
    "banned": 10,
    "shutdown": 10,
    "shut down": 10,
    "layoffs": 10,
    "hack": 10,
    "hacked": 10,
    "stolen": 10,
    "scam": 10,
    "fraud": 10,
    "warning": 8,
    "update": 5,
    "upgrade": 5,
    "download": 5,
    "launch": 4,
    "launched": 4,
    "released": 4,
    "open source": 6,
    "price hike": 12,
    "lawsuit": 8,
    "sued": 8,
    "settlement": 8,
    "billion": 10,
    "million": 8,
}

EMOTION_TERMS: dict[str, int] = {
    "critical": 10,
    "emergency": 10,
    "urgent": 10,
    "danger": 8,
    "controversial": 8,
    "backlash": 10,
    "outrage": 10,
    "surprising": 6,
    "record": 6,
    "biggest": 6,
    "fastest": 5,
    "cheapest": 5,
    "banned": 10,
    "killed": 8,
    "leak": 8,
    "leaked": 8,
}

ACTION_TERMS: dict[str, int] = {
    "update": 6,
    "patch": 8,
    "check": 5,
    "disable": 6,
    "delete": 6,
    "download": 5,
    "enable": 5,
    "migrate": 5,
    "upgrade": 6,
    "turn off": 6,
    "fix": 4,
}

CONSUMER_TERMS = {
    "you",
    "your",
    "users",
    "people",
    "everyone",
    "phone",
    "pc",
    "browser",
    "app",
    "account",
    "password",
    "email",
    "photos",
    "files",
    "money",
    "bank",
    "school",
    "work",
}

NICHE_TERMS = {
    "arista",
    "velocloud",
    "orchestrator",
    "federal agencies",
    "federal civilian",
    "nvd",
    "kubernetes",
    "terraform",
    "postgresql",
    "mysql",
    "redis",
    "apache",
    "tomcat",
    "jenkins",
    "enterprise",
    "on-prem",
    "on-premises",
    "sysadmin",
    "library",
    "runtime",
    "sdk",
    "patch management",
    "vulnerability management",
    "self-hosted",
}

NUMBER_RE = re.compile(
    r"(?:\bCVE-\d{4}-\d{4,}\b|\bCVSS\b|\$\s?\d+|\b\d+(?:\.\d+)*%?\b|"
    r"\b(?:million|billion)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ViralityScore:
    score: int
    audience: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "audience": self.audience,
            "reasons": list(self.reasons),
        }


def _matches(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))


def _hits(text: str, terms: dict[str, int], limit: int) -> tuple[int, list[str]]:
    total = 0
    found: list[str] = []
    for term, weight in terms.items():
        if _matches(text, term):
            total += weight
            found.append(term)
    return min(total, limit), found


def brand_strength(text: str) -> int:
    lowered = normalize_space(text).lower()
    weights = [weight for brand, weight in MASS_BRANDS.items() if _matches(lowered, brand)]
    if not weights:
        return 0
    return max(weights)


def is_mainstream_target(text: str) -> bool:
    return brand_strength(text) >= 24


def score_virality(item: StoryItem) -> ViralityScore:
    text = normalize_space(f"{item.title} {item.summary}").lower()
    score = 0
    reasons: list[str] = []

    brand_hits = [
        (brand, weight) for brand, weight in MASS_BRANDS.items() if _matches(text, brand)
    ]
    if brand_hits:
        brand_hits.sort(key=lambda pair: pair[1], reverse=True)
        brand_score = brand_hits[0][1] + min(4, max(0, len(brand_hits) - 1) * 2)
        score += brand_score
        reasons.append("mainstream brand: " + ", ".join(brand for brand, _ in brand_hits[:3]))

    impact_score, impact_hits = _hits(text, BROAD_IMPACT, 22)
    if impact_score:
        score += impact_score
        reasons.append("broad impact: " + ", ".join(impact_hits[:3]))

    emotion_score, emotion_hits = _hits(text, EMOTION_TERMS, 14)
    if emotion_score:
        score += emotion_score
        reasons.append("stakes/emotion: " + ", ".join(emotion_hits[:3]))

    action_score, action_hits = _hits(text, ACTION_TERMS, 10)
    if action_score:
        score += action_score
        reasons.append("actionable: " + ", ".join(action_hits[:3]))

    consumer_hits = [term for term in CONSUMER_TERMS if _matches(text, term)]
    if consumer_hits:
        score += min(6, len(consumer_hits) * 2)
        reasons.append("reader impact: " + ", ".join(consumer_hits[:3]))

    if NUMBER_RE.search(text):
        score += 5
        reasons.append("concrete number or CVE")

    niche_hits = [term for term in NICHE_TERMS if _matches(text, term)]
    if niche_hits:
        penalty = min(21, len(niche_hits) * 7)
        score -= penalty
        reasons.append("niche: " + ", ".join(niche_hits[:3]))

    has_mainstream_brand = bool(brand_hits)
    if item.source in {"NVD", "CISA KEV"} and not has_mainstream_brand:
        score -= 12
        reasons.append("niche-only security source")
    if "cisa" in text and not has_mainstream_brand:
        score -= 8
        reasons.append("CISA-only impact")
    if item.source_kind == "devto":
        score -= 8
        reasons.append("low-signal community post")
    if item.category == "security" and not has_mainstream_brand:
        score -= 5
        reasons.append("security-only audience")
    if len(normalize_space(item.summary)) < 50:
        score -= 10
        reasons.append("thin summary")

    score = max(0, min(100, score))
    if score >= 50 or (has_mainstream_brand and score >= 35):
        audience = "mass"
    elif score >= 30:
        audience = "tech"
    else:
        audience = "niche"
    return ViralityScore(score=score, audience=audience, reasons=tuple(reasons[:6]))
