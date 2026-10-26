from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai import generate_json
from app.config import Settings
from app.models import PostDraft, SourceRef, Story
from app.virality import is_mainstream_target
from app.text import (
    clean_body,
    clean_markdown,
    ensure_period,
    has_concrete_detail,
    hashtag_for_category,
    normalize_space,
    sentence_case,
    source_line,
    split_sentences,
    truncate_words,
)

logger = logging.getLogger(__name__)

TARGET_MIN = 190
TARGET_MAX = 300
BODY_TARGET_MAX = 245

NUMBER_CONTEXT_RE = re.compile(
    r"\b(\d[\d,]*(?:\.\d+)?%?(?:\s+(?:million|billion))?)\s+"
    r"([A-Za-z][A-Za-z-]+(?:\s+[A-Za-z][A-Za-z-]+){0,2})"
)

COUNT_FACT_UNITS = {
    "account", "accounts", "advertiser", "advertisers", "business", "businesses",
    "camera", "cameras", "company", "companies", "country", "countries",
    "customer", "customers", "developer", "developers", "device", "devices",
    "employee", "employees", "market", "markets", "model", "models",
    "people", "person", "phone", "phones", "subscriber", "subscribers",
    "user", "users", "worker", "workers", "percent", "million", "billion",
    "trillion",
}

SYSTEM_PROMPT = """You are a sharp tech news editor writing for Bluesky.
Write posts that make a general tech audience stop, understand why it matters, and share it.

Hard rules:
- No first-person (I, we, our, my, me). No invented persona, jokes, slang, or unverified hype.
- Use only facts supplied in the input. Never invent numbers, versions, dates, quotes, or impact.
- 3 to 5 sentences. Active voice. One idea per sentence. 20 words maximum per sentence.
- Open with the consequence for the reader, not the company announcement. "If you use X, ..." is strong when true.
- Never open with "announced", "in a major development", or a generic product name.
- Include the strongest concrete number, version, CVE, date, or price in the first half.
- For security news, say who is affected and the exact action to take.
- For product news, say what changes for users, not just what launched.
- Expand an acronym on first use.
- Attribute vendor claims: "PHP.net says...", "OpenAI says...".
- No URLs. No source line. No hashtags. No emojis. No rhetorical questions.
- Body must be 170-235 characters. A source line and one hashtag are added separately.
- If the facts cannot support a specific, accurate hook, return {"skip": true, "reason": "..."}.

Image metadata:
- image_title: headline, 2-6 words, factual, no clickbait.
- image_facts: 1-3 short facts, each 3-55 characters, taken from the supplied facts. Put the strongest number/impact first.
- alt_text: 1-2 sentences describing the card and its key facts.

Return JSON only:
{"body": "...", "image_title": "...", "image_facts": ["..."], "alt_text": "..."}"""


class WriterError(RuntimeError):
    pass


def _ai_available(settings: Settings) -> bool:
    return bool(
        settings.gemini_api_key
        or settings.groq_api_key
        or settings.openrouter_api_key
    )


def _user_prompt(story: Story) -> str:
    facts = {
        "title": story.title,
        "summary": truncate_words(story.summary, 1200),
        "category": story.category,
        "source_name": story.primary_source.name,
        "source_url": story.primary_source.url,
        "published_at": story.item.published_at.isoformat(),
        "is_breaking": story.is_breaking,
        "facts": story.facts,
    }
    return "Write the post from these supplied facts only:\n" + json.dumps(
        facts, ensure_ascii=False, indent=2, default=str
    )


def _clean_title(title: str) -> str:
    value = clean_markdown(title)
    value = re.sub(r"\s+[-|:]\s+(?:The Verge|Ars Technica|TechCrunch|Wired|BBC|Engadget|Tom's Hardware)$", "", value, flags=re.I)
    value = value.strip(" \"'“”‘’")
    value = value.rstrip("?")
    if value and not value.endswith((".", "!", "?")):
        value += "."
    return sentence_case(value)


def _story_content(story: Story) -> str:
    article = normalize_space(str(story.facts.get("article_text") or ""))
    return normalize_space(f"{story.summary} {article}")


_INCOMPLETE_ENDINGS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "had", "has",
    "can", "could", "have", "in", "is", "may", "might", "must", "no", "of",
    "on", "one", "or", "should", "that", "the", "to", "was", "were", "which",
    "will", "with", "would",
}


def _looks_complete(sentence: str) -> bool:
    words = sentence.rstrip(".!?").split()
    if len(words) < 6:
        return False
    if words[-1].lower().strip(",;:") in _INCOMPLETE_ENDINGS:
        return False
    if re.search(r"\b(?:a|an)\s+[A-Z][a-z]+\.$", sentence):
        return False
    if re.match(r"^(?:if|when|while|because|as)\b", sentence, re.IGNORECASE) and "," not in sentence:
        return False
    return True


def _first_summary_sentences(story: Story) -> list[str]:
    sentences = split_sentences(clean_body(_story_content(story)))
    cleaned: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        sentence = re.sub(
            r"^(?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{1,2},\s+\d{4}\s+",
            "",
            sentence,
        )
        sentence = ensure_period(sentence)
        if len(sentence) < 20 or not _looks_complete(sentence):
            continue
        key = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
        if key in seen:
            continue
        if cleaned:
            tokens = {token for token in key.split() if len(token) > 3}
            prior = {
                token
                for token in re.sub(
                    r"[^a-z0-9]+", " ", cleaned[-1].lower()
                ).split()
                if len(token) > 3
            }
            if tokens and prior and len(tokens & prior) / min(len(tokens), len(prior)) >= 0.8:
                continue
        seen.add(key)
        cleaned.append(sentence)
    return cleaned


def _cisa_body(story: Story) -> tuple[str, list[str]]:
    facts = story.facts
    vendor = normalize_space(str(facts.get("vendor") or ""))
    product = normalize_space(str(facts.get("product") or ""))
    cve = normalize_space(str(facts.get("cve") or ""))
    cvss = facts.get("cvss")
    severity = normalize_space(str(facts.get("severity") or "")).upper()
    due_date = normalize_space(str(facts.get("due_date") or ""))

    if product and vendor and (
        product.lower() == vendor.lower()
        or product.lower().startswith(vendor.lower() + " ")
    ):
        target = product
    else:
        target = " ".join(part for part in (vendor, product) if part)
    target = target or "affected software"
    flaw = cve or "the flaw"
    if target == "affected software":
        lead = "If you run affected software, patch now."
    elif is_mainstream_target(target):
        lead = f"If you use {target}, patch {flaw} now."
    else:
        lead = f"If your organization runs {target}, treat {flaw} as urgent."
    sentences: list[str] = [lead]
    sentences.append("CISA confirms active exploitation.")
    if cvss is not None:
        rating = f"NVD rates the flaw {cvss}"
        if severity:
            rating += f" ({severity})"
        sentences.append(rating + ".")
    if due_date:
        sentences.append(
            f"CISA set a {due_date} remediation deadline for US federal agencies."
        )
    else:
        sentences.append("It is listed in CISA's Known Exploited Vulnerabilities catalog.")

    body = " ".join(sentences[:4])
    image_title = truncate_words(f"CISA: {target} Exploited", 55)
    image_facts: list[str] = []
    if cvss is not None:
        image_facts.append(f"CVSS {cvss}")
    image_facts.append(cve)
    if due_date:
        image_facts.append(f"Due {due_date}")
    return body, image_facts


def _impact_sentence(story: Story) -> str:
    text = normalize_space(f"{story.title} {story.summary}").lower()
    if "gmail" in text and re.search(r"\b(?:ad|ads|advertising)\b", text):
        return "That could change which ads Gmail users see."
    if "phone number" in text and "account" in text:
        return "That links the number to every Google account on the device."
    if re.search(r"\b(?:privacy|data|personal intelligence)\b", text):
        return "The change affects how user data is handled across Google services."
    if "password" in text or "credential" in text:
        return "Anyone using the affected service should rotate credentials."
    if story.category == "security":
        return "Affected users should patch or mitigate as soon as possible."
    if story.category == "ai":
        return "The change affects how people use these AI tools."
    if story.category in {"hardware", "bigtech"}:
        return "The change affects users of the product or service."
    if story.category == "dev":
        return "Developers should check the release notes before upgrading."
    return "The change is detailed in the linked report."


def _generic_body(story: Story) -> tuple[str, list[str]]:
    title = _clean_title(story.title)
    summary_sentences = _first_summary_sentences(story)
    body_sentences: list[str] = []

    title_text = title.rstrip(".").lower()
    first_summary = summary_sentences[0].lower() if summary_sentences else ""
    if not first_summary or (
        title_text not in first_summary and first_summary not in title_text
    ):
        body_sentences.append(title)
    for sentence in summary_sentences:
        if len(body_sentences) >= 3:
            break
        if sentence.lower() not in {item.lower() for item in body_sentences}:
            body_sentences.append(sentence)
    if not body_sentences:
        body_sentences.append(title)

    if len(body_sentences) < 3:
        numeric = _numeric_context_facts(story)
        if numeric:
            body_sentences.append(
                f"The announcement cites {' and '.join(numeric[:2])}."
            )
    if len(body_sentences) < 3:
        body_sentences.append(_impact_sentence(story))
    if len(body_sentences) < 3:
        if story.category == "security":
            body_sentences.append("Affected systems should be patched or mitigated.")
        elif story.category == "dev":
            body_sentences.append("Release notes and upgrade details are available at the source.")
        elif story.category == "ai":
            body_sentences.append("Availability and access details are listed in the announcement.")
        elif story.category in {"hardware", "bigtech"}:
            body_sentences.append("Pricing, specifications, and availability are listed at the source.")
        elif story.category == "startups":
            body_sentences.append("Financial terms and launch details are listed at the source.")
        else:
            body_sentences.append("The linked source has the full details.")
    if len(body_sentences) < 3:
        body_sentences.append("The linked source has the full report.")

    body = " ".join(body_sentences[:4])
    image_title = _image_title_from_story(story)
    image_facts = _extract_image_facts(story, body, image_title)
    return body, image_facts


def _valid_count_fact(number: str, phrase: str) -> bool:
    if number.endswith("%") or re.search(r"\b(?:million|billion|trillion)\b", number, re.I):
        return True
    words = {word.lower().strip(".,;:") for word in phrase.split()}
    return bool(words & COUNT_FACT_UNITS)


def _numeric_context_facts(story: Story) -> list[str]:
    text = normalize_space(f"{story.title} {_story_content(story)}")
    facts: list[str] = []
    for match in NUMBER_CONTEXT_RE.finditer(text):
        number, phrase = match.group(1), match.group(2)
        words = phrase.split()
        while words and words[-1].lower() in _TRAILING_STOPWORDS | {
            "have", "has", "had", "used", "reach", "more", "than", "over",
        }:
            words.pop()
        if not words:
            continue
        if not _valid_count_fact(number, " ".join(words)):
            continue
        candidate = f"{number} {' '.join(words)}"
        if candidate.lower() not in {fact.lower() for fact in facts}:
            facts.append(candidate)
        if len(facts) >= 2:
            break
    return facts


def _image_title_from_story(story: Story) -> str:
    title = _clean_title(story.title).rstrip(".")
    text = f"{title} {story.summary}".lower()
    words = title.split()
    product = ""
    if words:
        first_word = words[0]
        if re.search(r"\d", first_word):
            product = " ".join(words[:2])
        else:
            product = " ".join(words[:2]) if len(words) > 1 else first_word
    product = product.strip(" ,.:;-")

    if story.facts.get("cisa"):
        target = normalize_space(
            f"{story.facts.get('product') or story.facts.get('vendor') or product}"
        )
        return truncate_words(f"CISA: {target} Exploited", 55)

    if story.category == "security" or re.search(
        r"\b(?:cve-\d{4}-\d{4,}|vulnerability|rce|remote code execution|security)\b",
        text,
        re.IGNORECASE,
    ):
        if re.search(r"\b(?:rce|remote code execution)\b", text, re.IGNORECASE):
            return truncate_words(f"{product} RCE: Patch Now", 55)
        return truncate_words(f"{product} Security Fix", 55)

    version_match = re.search(r"\b\d+\.\d+(?:\.\d+)?\b", title)
    if version_match and re.search(r"\b(?:release|released|version|update)\b", text, re.I):
        return truncate_words(f"{product} {version_match.group(0)} Released", 55)

    return _short_image_title(title)


_TRAILING_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is",
    "of", "on", "or", "the", "to", "with", "without", "your", "its",
}


def _short_image_title(title: str, max_words: int = 8) -> str:
    title = normalize_space(title).rstrip(".")
    if len(title.split()) <= max_words:
        return title

    match = re.match(r"^(.+?)\s+expands?\s+to\s+(.+)$", title, re.I)
    if match:
        tail = " ".join(match.group(2).split()[:3])
        candidate = f"{match.group(1)}: {tail}"
        if len(candidate.split()) <= max_words:
            return candidate

    match = re.match(r"^(.+?)\s+(?:is |are )?coming to\s+(.+)$", title, re.I)
    if match:
        candidate = f"{match.group(1)}: {match.group(2)}"
        if len(candidate.split()) <= max_words:
            return candidate

    match = re.match(
        r"^(?:Updated\s+)?(\w+)\s+privacy policy says (?:your\s+)?(.+?)\s+"
        r"(?:might|may|could|will)\s+be used for (advertising|ads)$",
        title,
        re.I,
    )
    if match:
        return f"{match.group(1)}: {match.group(2)} may be used for ads"

    match = re.match(
        r"^(.+?)\s+is quietly linking (?:your\s+)?(phone number)\s+to\s+"
        r"(?:other\s+)?(.+accounts)$",
        title,
        re.I,
    )
    if match:
        return f"{match.group(1)}: {sentence_case(match.group(2))} linked to {sentence_case(match.group(3))}"

    for separator in (" as ", " after ", " because ", " while ", " — ", " - ", ", "):
        prefix = title.split(separator, 1)[0].strip()
        if 3 <= len(prefix.split()) <= max_words:
            return prefix

    words = title.split()[:max_words]
    while words and words[-1].lower() in _TRAILING_STOPWORDS:
        words.pop()
    return " ".join(words) or truncate_words(title, 55)


def _title_image_fact(title: str) -> str:
    match = re.search(
        r"\b(?:link(?:ing|s|ed)?|tie[sd]?|attach(?:ing|es|ed)?)\s+"
        r"(?:your\s+)?(phone number)\s+to\s+(?:all\s+|other\s+)?(.+?accounts)\b",
        title,
        re.IGNORECASE,
    )
    if match:
        return f"Phone number linked to {sentence_case(match.group(2))}"
    match = re.search(
        r"\b(your\s+[A-Za-z][A-Za-z ]+?)\s+(?:might|may|could|will)\s+"
        r"be\s+used\s+for\s+(advertising|ads)\b",
        title,
        re.IGNORECASE,
    )
    if match:
        name = match.group(1)
        name = re.sub(r"^your\s+", "", name, flags=re.IGNORECASE)
        return f"{sentence_case(name)} may be used for ads"
    return ""


def _body_image_fact(story: Story, body: str) -> str:
    text = normalize_space(f"{story.summary} {body}")
    match = re.search(r"more data for ([a-z][a-z ]{2,28})", text, re.I)
    if match:
        return f"More data for {match.group(1).strip()}"
    match = re.search(
        r"\b(?:on|enabled|active|available|default|in place)\b[^.]{0,45}?"
        r"\bsince\s+([A-Z][a-z]+ \d{4})\b",
        text,
    )
    if match:
        prefix = "On by default" if "default" in match.group(0).lower() else "In place"
        return f"{prefix} since {match.group(1)}"
    match = re.search(
        r"\b(?:more than|over)\s+(?:a|one|two|three|\d+)\s+(?:year|month|week)s?\b",
        text,
        re.I,
    )
    if match:
        return sentence_case(match.group(0))
    return ""


def _fact_repeats_title(fact: str, image_title: str) -> bool:
    fact_tokens = {
        token for token in re.findall(r"[a-z0-9]+", fact.lower()) if len(token) > 2
    }
    title_tokens = set(re.findall(r"[a-z0-9]+", image_title.lower()))
    if not fact_tokens:
        return True
    return len(fact_tokens & title_tokens) / len(fact_tokens) >= 0.8


def _extract_image_facts(
    story: Story, body: str, image_title: str = ""
) -> list[str]:
    facts: list[str] = []
    text = f"{story.title} {_story_content(story)} {body}"
    cves = []
    for match in re.findall(r"\bCVE-\d{4}-\d{4,}\b", text, re.I):
        upper = match.upper()
        if upper not in cves:
            cves.append(upper)
    cvss = story.facts.get("cvss")
    if cvss is not None:
        facts.append(f"CVSS {cvss}")
    if cves:
        facts.append(" / ".join(cves[:2]))
    versions = []
    for match in re.findall(r"\b\d+\.\d+(?:\.\d+)?\b", text):
        if match not in versions:
            versions.append(match)
    if versions:
        facts.append("Fixed: " + ", ".join(versions[:4]))
    if story.facts.get("cisa") and story.facts.get("due_date"):
        facts.append(f"Due {story.facts['due_date']}")
    for key, label in (("price", "Price"), ("version", "Version")):
        value = normalize_space(str(story.facts.get(key) or ""))
        candidate = f"{label}: {value}" if value else ""
        if candidate and len(candidate) <= 60 and candidate not in facts:
            facts.append(candidate)

    if len(facts) < 3:
        seen_numbers = " ".join(facts)
        title_numbers = set(re.findall(r"\d[\d,.]*", image_title))
        for match in NUMBER_CONTEXT_RE.finditer(text):
            number, phrase = match.group(1), match.group(2)
            words = phrase.split()
            while words and words[-1].lower() in {
                "as", "have", "has", "had", "used", "to", "in", "on", "for",
                "with", "and", "or", "the", "a", "an", "is", "are", "was", "were",
                "by", "from", "over", "more", "than",
            }:
                words.pop()
            if not words:
                continue
            if not _valid_count_fact(number, " ".join(words)):
                continue
            candidate = f"{number} {' '.join(words)}"
            if len(candidate) > 60 or candidate in facts:
                continue
            if number in seen_numbers:
                continue
            number_key = re.sub(r"[^0-9]", "", number)
            if any(number_key == re.sub(r"[^0-9]", "", token) for token in title_numbers):
                continue
            facts.append(candidate)
            if len(facts) >= 3:
                break

    if len(facts) < 2:
        title_tokens = set(re.findall(r"[a-z0-9]+", story.title.lower()))
        for sentence in _first_summary_sentences(story):
            sentence_tokens = set(re.findall(r"[a-z0-9]+", sentence.lower()))
            if not sentence_tokens:
                continue
            overlap = len(title_tokens & sentence_tokens) / len(sentence_tokens)
            if overlap >= 0.7 or len(sentence) > 60:
                continue
            candidate = truncate_words(sentence, 55)
            if candidate and candidate not in facts:
                facts.append(candidate)
            if len(facts) >= 3:
                break
    facts = [fact for fact in facts if not _fact_repeats_title(fact, image_title)]
    if not facts:
        body_fact = _body_image_fact(story, body)
        if body_fact:
            facts.append(body_fact)
    if not facts:
        title_fact = _title_image_fact(story.title)
        if title_fact and not _fact_repeats_title(title_fact, image_title):
            facts.append(title_fact)
    if not facts:
        sentence = split_sentences(body)
        if sentence:
            facts.append(truncate_words(sentence[0], 55))
    return facts[:3]


def _alt_text(story: Story, image_title: str, image_facts: list[str]) -> str:
    source = story.primary_source.name
    facts = "; ".join(image_facts)
    return truncate_words(
        f"{story.category.title()} news card. {image_title}. {facts}. Source: {source}.",
        280,
    )


def _compose(
    body: str,
    refs: list[SourceRef],
    category: str,
    *,
    include_hashtag: bool = True,
    include_short: bool = True,
) -> tuple[str, str, list[str]]:
    body = clean_body(body)
    body = re.sub(r"^(?:post|body)\s*:\s*", "", body, flags=re.I)
    body = body.strip(" \"'“”‘’")
    body = normalize_space(body)
    if body and body[-1] not in ".!?":
        body += "."
    hashtag = hashtag_for_category(category)
    hashtags = [hashtag] if include_hashtag else []
    source = source_line(refs, include_short=include_short)
    parts = [body]
    if hashtags:
        parts.append(hashtag)
    if source:
        parts.append(source)
    text = "\n".join(part for part in parts if part)
    return text, body, hashtags


def _shorten_sentence(sentence: str, max_words: int) -> str:
    words = sentence.split()
    if len(words) <= max_words:
        return sentence

    # Prefer dropping a trailing explanatory clause over cutting mid-clause.
    clause_patterns = (
        r",\s+(?:which|helping|including|according to|while|because|as|but|and)\b",
        r";\s+",
        r":\s+",
    )
    for pattern in clause_patterns:
        prefix = re.split(pattern, sentence, maxsplit=1)[0].strip()
        if 5 <= len(prefix.split()) <= max_words:
            candidate = ensure_period(prefix)
            if _looks_complete(candidate):
                return candidate

    # Keep the longest comma-separated prefix that still fits.
    pieces = re.split(r",\s*", sentence)
    prefix = ""
    for piece in pieces:
        candidate = f"{prefix}, {piece}".strip(", ") if prefix else piece
        if len(candidate.split()) <= max_words:
            prefix = candidate
        else:
            break
    if len(prefix.split()) >= 5:
        candidate = ensure_period(prefix)
        if _looks_complete(candidate):
            return candidate

    for count in range(max_words, 4, -1):
        trimmed = " ".join(words[:count]).rstrip(" ,;:-")
        candidate = ensure_period(trimmed)
        if _looks_complete(candidate):
            return candidate
    trimmed = " ".join(words[:max_words]).rstrip(" ,;:-")
    return ensure_period(trimmed)


def _fit_text(
    body: str, refs: list[SourceRef], category: str
) -> tuple[str, str, list[str]]:
    body = clean_body(body)
    sentences = split_sentences(body)
    working = sentences[:5] if sentences else [body]
    if len(working) > 5:
        working = working[:5]

    def assemble(
        include_hashtag: bool = True, include_short: bool = True
    ) -> tuple[str, str, list[str]]:
        candidate_body = " ".join(working).strip()
        return _compose(
            candidate_body,
            refs,
            category,
            include_hashtag=include_hashtag,
            include_short=include_short,
        )

    text, candidate_body, hashtags = assemble()
    if len(text) <= TARGET_MAX:
        return text, candidate_body, hashtags

    # Remove optional hashtag first.
    text, candidate_body, hashtags = assemble(include_hashtag=False)
    if len(text) <= TARGET_MAX:
        return text, candidate_body, []

    # Remove the visible short URL but keep the clickable source name.
    text, candidate_body, hashtags = assemble(
        include_hashtag=False, include_short=False
    )
    if len(text) <= TARGET_MAX:
        return text, candidate_body, []

    # Keep at least three sentences, but shorten the longest sentence until it fits.
    if len(working) >= 3:
        while len(text) > TARGET_MAX:
            index = max(range(len(working)), key=lambda item: len(working[item].split()))
            word_count = len(working[index].split())
            if word_count <= 7:
                break
            working[index] = _shorten_sentence(
                working[index], max(7, word_count - max(2, word_count // 5))
            )
            text, candidate_body, hashtags = assemble(include_hashtag=False)
        if len(text) <= TARGET_MAX:
            return text, candidate_body, []

    # Last resort: trim the body, never the source line.
    source = source_line(refs, include_short=False)
    available = TARGET_MAX - (len(source) + 1 if source else 0)
    candidate_body = truncate_words(body, max(120, available))
    text = f"{candidate_body}\n{source}" if source else candidate_body
    return text[:TARGET_MAX], candidate_body, []


def _context_candidates(story: Story) -> list[str]:
    candidates: list[str] = []
    for key in ("nvd_description", "required_action", "release_name"):
        value = story.facts.get(key)
        if value:
            candidates.extend(split_sentences(clean_body(str(value))))
    candidates.extend(_first_summary_sentences(story))
    return [ensure_period(candidate) for candidate in candidates if len(candidate) >= 30]


def _hashtags_in_text(text: str) -> list[str]:
    return re.findall(r"(?<!\w)#[A-Za-z0-9_]+", text)


def _ensure_minimum(text: str, body: str, refs: list[SourceRef], category: str, story: Story) -> tuple[str, str, list[str]]:
    sentences = split_sentences(body)
    has_enough = (
        len(text) >= TARGET_MIN
        and 3 <= len(sentences) <= 5
        and has_concrete_detail(body)
    )
    if has_enough:
        return text, body, _hashtags_in_text(text)

    candidates = list(_context_candidates(story))
    published = story.item.published_at
    if published:
        candidates.append(f"The report was published on {published.astimezone().date().isoformat()}.")

    for candidate in candidates:
        if candidate.lower() in body.lower():
            continue
        candidate_body = f"{body} {candidate}".strip()
        candidate_text, candidate_body, hashtags = _fit_text(candidate_body, refs, category)
        candidate_sentences = split_sentences(candidate_body)
        if (
            TARGET_MIN <= len(candidate_text) <= TARGET_MAX
            and 3 <= len(candidate_sentences) <= 5
            and has_concrete_detail(candidate_body)
        ):
            return candidate_text, candidate_body, hashtags
    return text, body, _hashtags_in_text(text)


def _from_ai_result(
    story: Story, refs: list[SourceRef], payload: dict[str, Any], provider: str
) -> PostDraft | None:
    body = payload.get("body")
    if not isinstance(body, str) or not clean_body(body):
        return None
    body_sentences = split_sentences(clean_body(body))
    if len(body_sentences) < 3 or any(
        not _looks_complete(sentence) for sentence in body_sentences
    ):
        return None
    image_title = payload.get("image_title")
    image_facts = payload.get("image_facts")
    alt_text = payload.get("alt_text")
    if not isinstance(image_title, str) or not isinstance(alt_text, str):
        return None
    if not isinstance(image_facts, list) or not all(isinstance(item, str) for item in image_facts):
        return None
    image_title = truncate_words(image_title, 55)
    image_facts = [truncate_words(item, 55) for item in image_facts[:3]]
    alt_text = truncate_words(alt_text, 280)
    if not image_facts:
        return None
    text, body_clean, hashtags = _fit_text(body, refs, story.category)
    text, body_clean, hashtags = _ensure_minimum(text, body_clean, refs, story.category, story)
    return PostDraft(
        text=text,
        body=body_clean,
        source_refs=refs,
        image_title=image_title,
        image_facts=image_facts,
        alt_text=alt_text,
        category=story.category,
        hashtags=hashtags,
        provider=provider,
    )


def _fallback(story: Story, refs: list[SourceRef]) -> PostDraft:
    if story.facts.get("cisa"):
        body, image_facts = _cisa_body(story)
    else:
        body, image_facts = _generic_body(story)
    image_title = _image_title_from_story(story)
    text, body_clean, hashtags = _fit_text(body, refs, story.category)
    text, body_clean, hashtags = _ensure_minimum(
        text, body_clean, refs, story.category, story
    )
    return PostDraft(
        text=text,
        body=body_clean,
        source_refs=refs,
        image_title=image_title,
        image_facts=image_facts,
        alt_text=_alt_text(story, image_title, image_facts),
        category=story.category,
        hashtags=hashtags,
        provider="template",
    )


class Writer:
    def __init__(self, settings: Settings, allow_ai: bool = True):
        self.settings = settings
        self.allow_ai = allow_ai

    def draft(self, story: Story) -> PostDraft:
        refs = story.sources[: self.settings.source_refs_limit]
        if not refs:
            raise WriterError("story has no sources")
        if self.allow_ai and _ai_available(self.settings):
            payload, provider = generate_json(
                self.settings, SYSTEM_PROMPT, _user_prompt(story)
            )
            if payload and payload.get("skip"):
                logger.info("AI requested skip: %s", payload.get("reason", "no reason"))
            elif payload:
                draft = _from_ai_result(story, refs, payload, provider)
                if draft:
                    return draft
                logger.warning("AI output failed structural checks; using template")
        return _fallback(story, refs)
