from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


def _extract_json(value: str) -> dict[str, Any] | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider: str,
) -> httpx.Response | None:
    for attempt in range(3):
        try:
            response = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("%s request failed: %s", provider, exc.__class__.__name__)
            return None
        if response.status_code == 200:
            return response
        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        logger.warning("%s returned HTTP %s", provider, response.status_code)
        return None
    return None


def _gemini(
    client: httpx.Client, settings: Settings, system: str, user: str
) -> dict[str, Any] | None:
    if not settings.gemini_api_key:
        return None
    for model in settings.gemini_models:
        if not model:
            continue
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        response = _post_with_retry(
            client,
            url,
            headers={
                "x-goog-api-key": settings.gemini_api_key,
                "Content-Type": "application/json",
            },
            payload={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.35,
                    "maxOutputTokens": 900,
                    "responseMimeType": "application/json",
                },
            },
            provider=f"Gemini ({model})",
        )
        if response is None:
            continue
        payload = response.json()
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            text = "\n".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError):
            continue
        result = _extract_json(text)
        if result:
            return result
    return None


def _openai_compatible(
    client: httpx.Client,
    *,
    provider: str,
    url: str,
    api_key: str,
    models: tuple[str, ...],
    system: str,
    user: str,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }
    for model in models:
        if not model:
            continue
        response = _post_with_retry(
            client,
            url,
            headers=headers,
            payload={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.35,
                "max_tokens": 900,
                "response_format": {"type": "json_object"},
            },
            provider=f"{provider} ({model})",
        )
        if response is None:
            continue
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            continue
        result = _extract_json(text)
        if result:
            return result
    return None


def generate_json(
    settings: Settings, system: str, user: str
) -> tuple[dict[str, Any] | None, str]:
    with httpx.Client(timeout=settings.ai_timeout) as client:
        result = _gemini(client, settings, system, user)
        if result:
            return result, "gemini"
        result = _openai_compatible(
            client,
            provider="Groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            api_key=settings.groq_api_key,
            models=settings.groq_models,
            system=system,
            user=user,
        )
        if result:
            return result, "groq"
        result = _openai_compatible(
            client,
            provider="OpenRouter",
            url="https://openrouter.ai/api/v1/chat/completions",
            api_key=settings.openrouter_api_key,
            models=settings.openrouter_models,
            system=system,
            user=user,
            extra_headers={
                "HTTP-Referer": "https://github.com/aman179102",
                "X-Title": "Aman Tech News",
            },
        )
        if result:
            return result, "openrouter"
    return None, "template"
