"""LLM provider stubs for future transcription and summarisation.

Phase A: no logic. These classes exist so a later phase can implement
transcription (OpenRouter) and summarisation / extraction (MiniMax M3)
without changing the call sites.

Design goals:

* Two concrete providers behind one base class.
* A factory in :func:`default_providers` returns whichever is configured
  via env vars; absent keys yield a no-op provider rather than an error.
* All methods return plain dataclasses so the API layer doesn't need to
  know which model produced them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Deadline:
    text: str
    due: Optional[str] = None  # ISO-8601 date if the model could extract one


@dataclass
class Summary:
    text: str
    key_points: list[str] = field(default_factory=list)
    deadlines: list[Deadline] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


class LLMProvider:
    """Abstract provider. ``name`` is the short identifier for logging."""
    name: str = "base"

    def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError

    def summarize(self, transcript: str) -> Summary:
        raise NotImplementedError

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise NotImplementedError


class _NoOpProvider(LLMProvider):
    """Returned when no API keys are configured. Always says "not configured"."""
    name = "noop"

    def transcribe(self, audio_path: Path) -> str:
        raise RuntimeError("no LLM provider configured")

    def summarize(self, transcript: str) -> Summary:
        raise RuntimeError("no LLM provider configured")

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise RuntimeError("no LLM provider configured")


class OpenRouterProvider(LLMProvider):
    """Will be implemented in a later phase. Key env var: LR_OPENROUTER_API_KEY."""
    name = "openrouter"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("OpenRouterProvider requires LR_OPENROUTER_API_KEY")
        self._api_key = api_key

    def transcribe(self, audio_path: Path) -> str:
        raise NotImplementedError("OpenRouter transcription comes in a later phase")


class MiniMaxProvider(LLMProvider):
    """Will be implemented in a later phase. Key env var: LR_MINIMAX_API_KEY."""
    name = "minimax"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("MiniMaxProvider requires LR_MINIMAX_API_KEY")
        self._api_key = api_key

    def summarize(self, transcript: str) -> Summary:
        raise NotImplementedError("MiniMax summarisation comes in a later phase")

    def extract(self, transcript: str, kind: str) -> list[dict]:
        raise NotImplementedError("MiniMax extraction comes in a later phase")


def default_providers() -> dict[str, LLMProvider]:
    """Return the provider registry based on current env vars.

    Phase A always returns a no-op registry. A later phase will
    instantiate real providers when their keys are present.
    """
    return {"default": _NoOpProvider()}


def get_provider(name: str = "default") -> LLMProvider:
    return default_providers().get(name) or _NoOpProvider()
