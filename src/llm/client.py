from __future__ import annotations

import os

from anthropic import AsyncAnthropic
from dotenv import load_dotenv


def create_async_anthropic_client() -> AsyncAnthropic:
    """Create an Anthropic SDK client from env vars with OpenRouter fallback."""
    load_dotenv()

    api_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("ANTHROPIC_AUTH_TOKEN")
        or os.getenv("OPENROUTER_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY or OPENROUTER_API_KEY")

    base_url = _normalize_base_url(os.getenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api"))
    return AsyncAnthropic(api_key=api_key, base_url=base_url)


def _normalize_base_url(raw_base_url: str) -> str:
    """
    Anthropic SDK appends `/v1/*` paths itself.
    For OpenRouter this means the base should be `https://openrouter.ai/api`,
    so requests become `https://openrouter.ai/api/v1/...`.
    """
    base = raw_base_url.rstrip("/")
    if base in {"https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/anthropic"}:
        return "https://openrouter.ai/api"
    return base
