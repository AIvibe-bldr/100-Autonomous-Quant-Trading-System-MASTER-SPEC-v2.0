"""Anthropic API client factory for the real Decision / Skeptic / Audit AI
adapters (MASTER SPEC §27-31, ADDENDUM A3).

Model selection follows the funnel's cost gradient (§21, §80): Decision AI
runs once per scanned candidate (~20 per session, after the Python quant
scan already filtered ~5000 symbols down), Skeptic AI only runs on
candidates Decision AI wants to BUY, and Audit AI only runs on orders that
already survived sizing — so call volume shrinks at each stage while model
capability can rise. Defaults reflect that:

- Decision AI: `claude-sonnet-5` — highest volume, cost-efficient default
- Skeptic AI: `claude-opus-5` — lower volume; a stronger adversarial reviewer
  catches more than Decision AI would catch of itself
- Audit AI: `claude-haiku-4-5` — narrow semantic-consistency check, not deep
  reasoning; fast and cheap, and naturally a different model family (A3-5)

All three are configurable via environment variables so the Operating Cost
/ Data ROI Engines (§80-82) can tune the mix without a code change.

This module never imports anything from `packages/broker_adapters` (§74:
AI never sees Broker credentials) — enforced structurally by not importing
it, and checked by the existing AST invariant test over `services/decision`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any


class LLMUnavailableError(RuntimeError):
    """No Anthropic credentials configured, or the SDK is not installed.

    Callers (IndependentAuditor, the pipeline's decision step) already treat
    an unavailable model as a hard stop for mandatory checks (INV-19) —
    this exception is what triggers that path for the real adapters.
    """


@dataclass(frozen=True)
class LLMModelConfig:
    decision_model: str = field(
        default_factory=lambda: os.environ.get("QUANT_DECISION_MODEL", "claude-sonnet-5"))
    skeptic_model: str = field(
        default_factory=lambda: os.environ.get("QUANT_SKEPTIC_MODEL", "claude-opus-5"))
    audit_model: str = field(
        default_factory=lambda: os.environ.get("QUANT_AUDIT_MODEL", "claude-haiku-4-5"))
    decision_max_tokens: int = 2048
    skeptic_max_tokens: int = 1024
    audit_max_tokens: int = 1024


DEFAULT_MODEL_CONFIG = LLMModelConfig()


def credentials_available() -> bool:
    """Best-effort pre-check for pluasible Anthropic credentials.

    Mirrors the SDK's own resolution order closely enough to let callers
    choose the Mock adapters instead of constructing a client that will
    immediately fail — the SDK itself remains the authority on whether a
    credential actually works.
    """
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_PROFILE")
        or os.path.exists(os.path.expanduser("~/.config/anthropic"))
    )


@lru_cache(maxsize=1)
def get_client() -> Any:
    """Lazily constructs a single shared Anthropic client.

    Cached so repeated calls across Decision/Skeptic/Audit adapters within
    one process reuse the same connection pool. Tests should never call
    this — inject a fake client into the adapters directly instead.
    """
    try:
        import anthropic
    except ImportError as e:
        raise LLMUnavailableError(
            "anthropic package not installed — run `pip install anthropic`") from e
    if not credentials_available():
        raise LLMUnavailableError(
            "no Anthropic credentials found — set ANTHROPIC_API_KEY or run "
            "`ant auth login`")
    return anthropic.Anthropic()
