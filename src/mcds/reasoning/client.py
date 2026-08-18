"""Anthropic client wiring and per-stage model routing.

The routing principle: models produce judgment, code produces numbers. Nothing
in scoring/ calls a model, and nothing here returns a figure the memo will
quote. See docs/model-routing.md for the full rationale per stage.

Request shapes differ meaningfully across the tiers used here, and getting them
wrong is a 400 rather than a silent degradation:

  claude-fable-5   thinking is always on -- OMIT the `thinking` parameter
                   entirely. Depth is controlled by output_config.effort, which
                   accepts low..max. `budget_tokens` and sampling parameters are
                   rejected. Unavailable under zero data retention.
  claude-opus-5    thinking adaptive by default; effort low..max.
  claude-sonnet-5  thinking: {"type": "adaptive"}; effort low..max.
  claude-haiku-4-5 older generation: thinking takes {"type": "enabled",
                   "budget_tokens": N} and `effort` is an error.

Fable 5 and Opus 5 can end a turn with stop_reason "refusal" (HTTP 200, not an
exception), so every call site checks stop_reason before reading content, and
server-side fallbacks are on by default so a refusal reroutes instead of
crashing a deal run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

Stage = Literal["normalize", "classify", "claims", "synthesize", "review", "triage"]

DEFAULT_MODELS: dict[str, str] = {
    "normalize": "claude-haiku-4-5",
    "classify": "claude-sonnet-5",
    "claims": "claude-sonnet-5",
    "synthesize": "claude-fable-5",
    "review": "claude-opus-5",
    "triage": "claude-sonnet-5",
}

DEFAULT_EFFORT: dict[str, str] = {
    "synthesize": "xhigh",
    "review": "high",
    "classify": "medium",
    "claims": "medium",
    "triage": "low",
}

#: Models on which `output_config.effort` is valid and `thinking` must not carry
#: a token budget.
ADAPTIVE_THINKING_MODELS = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5")

#: Thinking is unconditionally on here, so sending the parameter at all is a 400.
ALWAYS_THINKING_MODELS = ("claude-fable-5",)

#: Streaming avoids HTTP timeouts at the max_tokens the synthesis stage needs.
STREAMING_MAX_TOKENS = 64_000
NONSTREAMING_MAX_TOKENS = 16_000

SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RefusalError(RuntimeError):
    """Raised when a stage ends with stop_reason 'refusal' and no fallback applied."""


@dataclass
class RoutingConfig:
    models: dict[str, str]
    effort: dict[str, str]
    server_side_fallbacks: bool = True
    prompt_cache: bool = True
    org_requires_zdr: bool = False

    @classmethod
    def from_settings(cls, settings: dict | None = None) -> "RoutingConfig":
        settings = settings or {}
        models = {**DEFAULT_MODELS, **(settings.get("models") or {})}
        effort = {**DEFAULT_EFFORT, **(settings.get("effort") or {})}
        reasoning = settings.get("reasoning") or {}
        cfg = cls(
            models=models,
            effort=effort,
            server_side_fallbacks=reasoning.get("server_side_fallbacks", True),
            prompt_cache=reasoning.get("prompt_cache", True),
            org_requires_zdr=reasoning.get("org_requires_zdr", False),
        )
        if cfg.org_requires_zdr and cfg.models.get("synthesize") == "claude-fable-5":
            # Fable 5 is not offered under zero data retention. Degrade to the
            # next most capable model rather than failing the run at request time.
            cfg.models["synthesize"] = "claude-opus-5"
        return cfg

    def model_for(self, stage: Stage) -> str:
        return self.models[stage]


def build_request(
    stage: Stage,
    routing: RoutingConfig,
    *,
    system: str | list[dict],
    messages: list[dict],
    output_schema: dict | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
) -> dict:
    """Assemble the kwargs for a Messages request, correct for the target model.

    Kept separate from the call itself so the routing logic is unit-testable
    without an API key or a network.
    """
    model = routing.model_for(stage)
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens
        or (STREAMING_MAX_TOKENS if stream else NONSTREAMING_MAX_TOKENS),
        "system": system,
        "messages": messages,
    }

    output_config: dict[str, Any] = {}
    if model in ADAPTIVE_THINKING_MODELS:
        effort = routing.effort.get(stage)
        if effort:
            output_config["effort"] = effort
        if model not in ALWAYS_THINKING_MODELS:
            # Sending `thinking` to Fable 5 is a 400; everywhere else it is how
            # adaptive thinking is requested explicitly.
            req["thinking"] = {"type": "adaptive"}
    else:
        # Pre-4.6 generation: a fixed budget is the only way to get thinking,
        # and `effort` is rejected outright.
        req["thinking"] = {"type": "enabled", "budget_tokens": 4_000}

    if output_schema is not None:
        output_config["format"] = {
            "type": "json_schema",
            "schema": output_schema,
        }
    if output_config:
        req["output_config"] = output_config

    if routing.server_side_fallbacks and model in ("claude-fable-5", "claude-opus-5"):
        req["betas"] = [SERVER_SIDE_FALLBACK_BETA]
        req["fallbacks"] = "default"

    return req


def cached_system(stable_prefix: str, volatile_suffix: str | None = None) -> list[dict]:
    """System prompt split so the stable half caches across every deal in a batch.

    The rubric, the scoring definitions, and the vertical config are identical
    across a pipeline of deals, and they are long. Putting a cache breakpoint
    after them and the per-deal data behind it turns a 20-address screening run
    into one full-price call and nineteen cache reads.

    Anything volatile -- a timestamp, a deal id -- must land AFTER the
    breakpoint, or it invalidates the prefix on every call and the cache silently
    never hits. Verify with usage.cache_read_input_tokens, not by assumption.
    """
    blocks = [
        {
            "type": "text",
            "text": stable_prefix,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if volatile_suffix:
        blocks.append({"type": "text", "text": volatile_suffix})
    return blocks


def get_client(api_key: str | None = None):
    """Construct the Anthropic client.

    A bare constructor works when `ant auth login` has stored a profile, so an
    unset ANTHROPIC_API_KEY does not mean there are no credentials. Only pass a
    key explicitly when one is genuinely configured.
    """
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The `anthropic` package is required for the reasoning stages. "
            "Install it with `pip install anthropic`."
        ) from exc

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    return Anthropic(api_key=key) if key else Anthropic()


def check_refusal(response: Any) -> None:
    """Raise if a turn ended in a refusal rather than content.

    A refusal returns HTTP 200 with stop_reason 'refusal' and a stop_details
    category, so code that reads response.content without checking gets an
    empty or partial result and no error.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RefusalError(f"Stage refused (category={category}).")
