from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser() if raw else default


DEFAULT_GEMINI_MODELS = (
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-3.6-flash",
)
DEFAULT_GROQ_MODELS = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
)
DEFAULT_OPENROUTER_MODELS = (
    "openrouter/free",
    "google/gemma-4-26b-a4b-it:free",
    "thinkingmachines/inkling-small:free",
    "cohere/north-mini-code:free",
)


def _model_list(plural: str, singular: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(plural) or os.getenv(singular)
    values = tuple(value.strip() for value in (raw or "").split(",") if value.strip())
    return values or default


@dataclass(frozen=True)
class Settings:
    handle: str = ""
    app_password: str = ""
    pds_url: str = "https://bsky.social"

    gemini_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    gemini_models: tuple[str, ...] = DEFAULT_GEMINI_MODELS
    groq_models: tuple[str, ...] = DEFAULT_GROQ_MODELS
    openrouter_models: tuple[str, ...] = DEFAULT_OPENROUTER_MODELS

    github_token: str = ""
    nvd_api_key: str = ""
    healthcheck_url: str = ""

    state_dir: Path = field(default_factory=lambda: _path_from_env("STATE_DIR", ROOT / "state"))
    output_dir: Path = field(default_factory=lambda: _path_from_env("OUTPUT_DIR", ROOT / "output"))
    fonts_dir: Path = field(default_factory=lambda: ROOT / "assets" / "fonts")

    posting_enabled: bool = True
    post_with_image: bool = False
    max_posts_per_day: int = 4
    max_breaking_posts_per_day: int = 1
    max_breaking_posts_per_week: int = 3
    max_total_posts_per_day: int = 4
    history_days: int = 30
    timezone: str = "Asia/Kolkata"

    request_timeout: float = 15.0
    ai_timeout: float = 45.0
    max_source_items: int = 160
    min_virality_score: int = 30
    virality_weight: float = 1.6

    engagement_enabled: bool = False
    engagement_per_run: int = 1
    engagement_daily_limit: int = 30
    engagement_weekly_limit: int = 210
    engagement_author_cooldown_days: int = 7
    engagement_min_likes: int = 0
    engagement_max_replies: int = 50
    engagement_max_age_hours: int = 48
    engagement_topics: str = (
        "AI, OpenAI, Google, Microsoft, Apple, cybersecurity, Linux, Python, developer tools, "
        "CVE, vulnerability, data breach, Android, Chrome, cloud computing, AWS, Kubernetes, Rust, Golang"
    )
    engagement_require_ai: bool = True

    user_agent: str = (
        "Mozilla/5.0 (compatible; AmanTechNewsBot/1.0; "
        "+https://github.com/aman179102)"
    )

    @property
    def gemini_model(self) -> str:
        return self.gemini_models[0] if self.gemini_models else ""

    @property
    def groq_model(self) -> str:
        return self.groq_models[0] if self.groq_models else ""

    @property
    def openrouter_model(self) -> str:
        return self.openrouter_models[0] if self.openrouter_models else ""

    @property
    def source_refs_limit(self) -> int:
        return 2

    def require_bluesky(self) -> None:
        missing = []
        if not self.handle:
            missing.append("BSKY_HANDLE")
        if not self.app_password:
            missing.append("BSKY_APP_PASSWORD")
        if missing:
            raise ConfigError(
                "Missing Bluesky credentials: " + ", ".join(missing)
            )

    def require_posting_enabled(self) -> None:
        if not self.posting_enabled:
            raise ConfigError("Posting is disabled by POSTING_ENABLED")


def load_settings() -> Settings:
    return Settings(
        handle=os.getenv("BSKY_HANDLE", "").strip(),
        app_password=os.getenv("BSKY_APP_PASSWORD", "").strip(),
        pds_url=os.getenv("BSKY_PDS_URL", "https://bsky.social").rstrip("/"),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        gemini_models=_model_list(
            "GEMINI_MODELS", "GEMINI_MODEL", DEFAULT_GEMINI_MODELS
        ),
        groq_models=_model_list("GROQ_MODELS", "GROQ_MODEL", DEFAULT_GROQ_MODELS),
        openrouter_models=_model_list(
            "OPENROUTER_MODELS", "OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODELS
        ),
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        nvd_api_key=os.getenv("NVD_API_KEY", "").strip(),
        healthcheck_url=os.getenv("HEALTHCHECK_URL", "").strip(),
        state_dir=_path_from_env("STATE_DIR", ROOT / "state"),
        output_dir=_path_from_env("OUTPUT_DIR", ROOT / "output"),
        posting_enabled=env_bool("POSTING_ENABLED", True),
        post_with_image=env_bool("POST_WITH_IMAGE", False),
        max_posts_per_day=int(os.getenv("MAX_POSTS_PER_DAY", "4")),
        max_breaking_posts_per_day=int(os.getenv("MAX_BREAKING_POSTS_PER_DAY", "1")),
        max_breaking_posts_per_week=int(os.getenv("MAX_BREAKING_POSTS_PER_WEEK", "3")),
        max_total_posts_per_day=int(os.getenv("MAX_TOTAL_POSTS_PER_DAY", "4")),
        history_days=int(os.getenv("HISTORY_DAYS", "30")),
        timezone=os.getenv("TIMEZONE", "Asia/Kolkata").strip(),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "15")),
        ai_timeout=float(os.getenv("AI_TIMEOUT", "45")),
        max_source_items=int(os.getenv("MAX_SOURCE_ITEMS", "160")),
        min_virality_score=int(os.getenv("MIN_VIRALITY_SCORE", "30")),
        virality_weight=float(os.getenv("VIRALITY_WEIGHT", "1.6")),
        engagement_enabled=env_bool("ENGAGEMENT_ENABLED", False),
        engagement_per_run=int(os.getenv("ENGAGEMENT_PER_RUN", "1")),
        engagement_daily_limit=int(os.getenv("ENGAGEMENT_DAILY_LIMIT", "30")),
        engagement_weekly_limit=int(os.getenv("ENGAGEMENT_WEEKLY_LIMIT", "210")),
        engagement_author_cooldown_days=int(
            os.getenv("ENGAGEMENT_AUTHOR_COOLDOWN_DAYS", "7")
        ),
        engagement_min_likes=int(os.getenv("ENGAGEMENT_MIN_LIKES", "0")),
        engagement_max_replies=int(os.getenv("ENGAGEMENT_MAX_REPLIES", "50")),
        engagement_max_age_hours=int(os.getenv("ENGAGEMENT_MAX_AGE_HOURS", "48")),
        engagement_topics=os.getenv(
            "ENGAGEMENT_TOPICS",
            "AI, OpenAI, Google, Microsoft, Apple, cybersecurity, Linux, Python, developer tools, "
            "CVE, vulnerability, data breach, Android, Chrome, cloud computing, AWS, Kubernetes, Rust, Golang",
        ).strip(),
        engagement_require_ai=env_bool("ENGAGEMENT_REQUIRE_AI", True),
    )
