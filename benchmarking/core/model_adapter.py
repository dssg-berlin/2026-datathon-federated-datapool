"""Chat models for the benchmark.

- `LiteLLMChatModel`: any model LiteLLM supports (OpenAI, Anthropic, Gemini, OpenRouter,
  Ollama, vLLM, and Open WebUI through its OpenAI-compatible API). Configured in
  config/models.yaml. Records tokens, latency, time to first token and estimated cost.
- `OracleChatModel`: a deterministic stand-in that calls the case's expected tool with
  the expected arguments and answers with the tool output. It needs no API key and checks
  the pipeline (server, tools, evaluators, reports) end to end. Its scores are NOT model
  measurements.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from benchmarking.core.cases import EvalCase

DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant for the federated directory of social support services in "
    "Germany (facilities, services/offers, addresses and provider organisations from "
    "Caritas, AWO, DHS and others). Use the provided tools to look facts up; do not "
    "answer from memory. Search with German terms, since the data is German, and answer "
    "in the language of the user. Never invent phone numbers, email addresses, opening "
    "hours or facilities: if a field is not on record, say so. If nothing matches, say "
    "that nothing was found."
)

Tier = Literal["frontier", "fast", "local", "oracle"]


class ModelEntryConfig(BaseModel):
    """One model in models.yaml."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    litellm_model: str
    provider: str
    tier: Tier
    api_base: str | None = None
    api_base_env: str | None = Field(default=None, description="env var that overrides api_base")
    api_key_env: str | None = Field(default=None, description="env var holding the API key")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0)
    timeout_seconds: float = Field(default=120.0, gt=0)
    supports_tool_choice: bool = True
    cost_per_1k_input_usd: float = Field(default=0.0, ge=0.0)
    cost_per_1k_output_usd: float = Field(default=0.0, ge=0.0)
    extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description="provider-specific request parameters, e.g. {num_ctx: 16384} for Ollama")


class ModelTierGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    models: list[ModelEntryConfig] = Field(default_factory=list)


ORACLE = ModelEntryConfig(
    id="oracle", name="Oracle (pipeline check, not a model)", litellm_model="oracle",
    provider="none", tier="oracle",
)


class ModelsRegistryConfig(BaseModel):
    """config/models.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    default_tier: Literal["frontier", "fast", "local"] = "fast"
    tiers: dict[Literal["frontier", "fast", "local"], ModelTierGroup]

    @classmethod
    def from_yaml(cls, path: str | Path) -> ModelsRegistryConfig:
        with Path(path).open("r", encoding="utf-8") as f:
            return cls.model_validate(yaml.safe_load(f))

    def list_all_models(self, tier: str | None = None) -> list[ModelEntryConfig]:
        if tier is not None:
            return list(self.tiers[tier].models)
        return [m for group in self.tiers.values() for m in group.models]

    def get_model(self, key: str) -> ModelEntryConfig:
        """By id, name or LiteLLM string; "oracle" is built in; "litellm:<model>" is ad hoc."""
        target = key.strip()
        if target.lower() == "oracle":
            return ORACLE
        for m in self.list_all_models():
            if target.lower() in (m.id.lower(), m.name.lower(), m.litellm_model.lower()):
                return m
        if target.startswith("litellm:"):
            model = target.removeprefix("litellm:")
            return ModelEntryConfig(id=model, name=model, litellm_model=model, provider="adhoc", tier="fast")
        raise KeyError(f"model {key!r} is not in models.yaml (or use 'oracle' / 'litellm:<provider/model>')")

    def resolve(self, models_arg: str) -> list[ModelEntryConfig]:
        """--models: comma-separated ids, tiers (frontier/fast/local) or 'all'."""
        selected: list[ModelEntryConfig] = []
        for token in [t.strip() for t in models_arg.split(",") if t.strip()]:
            if token == "all":
                selected += self.list_all_models()
            elif token in self.tiers:
                selected += self.list_all_models(token)
            else:
                selected.append(self.get_model(token))
        return list({m.id: m for m in selected}.values())


class RetryPolicyConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_retries: int = Field(default=3, ge=0)
    initial_backoff_seconds: float = Field(default=1.0, gt=0)
    max_backoff_seconds: float = Field(default=16.0, gt=0)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)
    jitter: bool = True


class InterceptedToolCall(BaseModel):
    """A tool call the model asked for."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments_json: str = "{}"
    parse_error: str | None = None


class TokenUsageMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def __add__(self, other: TokenUsageMetrics) -> TokenUsageMetrics:
        return TokenUsageMetrics(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            estimated_cost_usd=round(self.estimated_cost_usd + other.estimated_cost_usd, 6),
        )


class ModelInvocationResult(BaseModel):
    """One model response (one step of the agent loop)."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    success: bool
    assistant_content: str | None = None
    intercepted_tool_calls: list[InterceptedToolCall] = Field(default_factory=list)
    usage: TokenUsageMetrics = Field(default_factory=TokenUsageMetrics)
    ttft_ms: float = 0.0
    roundtrip_latency_ms: float = 0.0
    finish_reason: str | None = None
    retries_attempted: int = 0
    error_message: str | None = None

    def assistant_message(self) -> dict[str, Any]:
        """The response as an OpenAI chat message, to append to the conversation."""
        message: dict[str, Any] = {"role": "assistant", "content": self.assistant_content or ""}
        if self.intercepted_tool_calls:
            message["tool_calls"] = [
                {"id": c.call_id, "type": "function",
                 "function": {"name": c.tool_name, "arguments": c.raw_arguments_json}}
                for c in self.intercepted_tool_calls
            ]
        return message


def parse_arguments(raw: Any) -> tuple[dict[str, Any], str, str | None]:
    """(arguments, raw JSON, parse error) from a tool call's arguments field."""
    if isinstance(raw, dict):
        return raw, json.dumps(raw, ensure_ascii=False), None
    text = str(raw or "{}").strip() or "{}"
    try:
        value = json.loads(text)
    except ValueError as exc:
        return {}, text, f"arguments are not valid JSON: {exc}"
    if not isinstance(value, dict):
        return {}, text, f"arguments must be a JSON object, got {type(value).__name__}"
    return value, text, None


class LiteLLMChatModel:
    """A models.yaml entry called through LiteLLM, with retries."""

    def __init__(self, cfg: ModelEntryConfig, retry: RetryPolicyConfig | None = None) -> None:
        self.cfg = cfg
        self.retry = retry or RetryPolicyConfig()

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelInvocationResult:
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm  # noqa: PLC0415 - optional dependency, only for real models

        litellm.drop_params = True
        kwargs: dict[str, Any] = {
            "model": self.cfg.litellm_model, "messages": messages,
            "temperature": self.cfg.temperature, "max_tokens": self.cfg.max_tokens,
            "timeout": self.cfg.timeout_seconds, **self.cfg.extra_params,
        }
        api_base = (os.environ.get(self.cfg.api_base_env) if self.cfg.api_base_env else None) or self.cfg.api_base
        if api_base:
            kwargs["api_base"] = api_base
        if self.cfg.api_key_env:
            key = os.environ.get(self.cfg.api_key_env)
            if not key:
                return self._failure(f"environment variable {self.cfg.api_key_env} is not set", 0.0, 0)
            kwargs["api_key"] = key
        if tools:
            kwargs["tools"] = tools
            if self.cfg.supports_tool_choice:
                kwargs["tool_choice"] = "auto"

        attempt, start, error = 0, time.perf_counter(), None
        while attempt <= self.retry.max_retries:
            call_start = time.perf_counter()
            try:
                response = await litellm.acompletion(**kwargs)
                return self._parse(response, (time.perf_counter() - call_start) * 1000.0, attempt, litellm, messages)
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                error = exc
                if attempt >= self.retry.max_retries:
                    break
                backoff = min(self.retry.max_backoff_seconds,
                              self.retry.initial_backoff_seconds * self.retry.backoff_multiplier ** attempt)
                await asyncio.sleep(backoff * (random.uniform(0.8, 1.2) if self.retry.jitter else 1.0))
                attempt += 1
        return self._failure(f"{type(error).__name__}: {error}", (time.perf_counter() - start) * 1000.0, attempt)

    def _failure(self, message: str, latency_ms: float, retries: int) -> ModelInvocationResult:
        return ModelInvocationResult(
            model_id=self.cfg.id, success=False, error_message=message,
            ttft_ms=latency_ms, roundtrip_latency_ms=latency_ms, retries_attempted=retries,
        )

    def _parse(self, response: Any, latency_ms: float, retries: int, litellm: Any, messages: list) -> ModelInvocationResult:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return self._failure("model returned no choices", latency_ms, retries)
        message = choices[0].message
        calls = []
        for index, call in enumerate(getattr(message, "tool_calls", None) or []):
            function = call.function if not isinstance(call, dict) else call["function"]
            name = function.name if not isinstance(function, dict) else function["name"]
            raw = function.arguments if not isinstance(function, dict) else function.get("arguments")
            arguments, raw_json, error = parse_arguments(raw)
            call_id = (call.id if not isinstance(call, dict) else call.get("id")) or f"call_{index}"
            calls.append(InterceptedToolCall(call_id=str(call_id), tool_name=str(name), arguments=arguments,
                                             raw_arguments_json=raw_json, parse_error=error))

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        if prompt_tokens == 0:
            try:
                prompt_tokens = int(litellm.token_counter(model=self.cfg.litellm_model, messages=messages))
            except Exception:  # noqa: BLE001 - token counts are best effort
                prompt_tokens = 0
        cost = (prompt_tokens / 1000.0) * self.cfg.cost_per_1k_input_usd + (
            completion_tokens / 1000.0) * self.cfg.cost_per_1k_output_usd

        return ModelInvocationResult(
            model_id=self.cfg.id, success=True, assistant_content=message.content,
            intercepted_tool_calls=calls,
            usage=TokenUsageMetrics(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                                    total_tokens=prompt_tokens + completion_tokens,
                                    estimated_cost_usd=round(cost, 6)),
            ttft_ms=round(latency_ms, 2), roundtrip_latency_ms=round(latency_ms, 2),
            finish_reason=getattr(choices[0], "finish_reason", None), retries_attempted=retries,
        )


def oracle_arguments(case: EvalCase, tool: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    """Concrete arguments that satisfy the case's expected arguments for `tool`."""

    prompt_iris = re.findall(r"https://civic-data\.de/federated-directory#[A-Za-z0-9_-]+", case.prompt)

    def concrete(value: Any) -> Any:
        if isinstance(value, dict) and "any_of" in value:
            return concrete(value["any_of"][0])
        if isinstance(value, list):
            return ", ".join(str(concrete(v)) for v in value)
        if isinstance(value, str):
            # Expected args are substrings ("org-34e9..."); use the full IRI from the prompt.
            for iri in prompt_iris:
                if value in iri:
                    return iri
        return value

    arguments = {key: concrete(value) for key, value in case.args_for(tool).items()}
    properties = (schema or {}).get("properties", {})
    for key in (schema or {}).get("required", []):
        if key not in arguments:
            # A required argument the case does not pin down: a plain string guess.
            kind = properties.get(key, {}).get("type")
            arguments[key] = 1 if kind == "integer" else "Berlin"
    return arguments


def _looks_empty(output: str) -> bool:
    """A tool result with no records: [], an error, or a zero total."""
    if output.startswith("ERROR") or output.startswith("No facility found"):
        return True
    try:
        value = json.loads(output)
    except ValueError:
        return False
    if value in ([], {}, 0):
        return True
    if isinstance(value, dict):
        if "error" in value or value.get("status") in ("not_found", "invalid_iri", "error"):
            return True
        for key in ("total", "total_found", "total_matches"):
            if value.get(key) == 0:
                return True
    return False


class OracleChatModel:
    """Calls the expected tool with the expected arguments, then answers with its output."""

    cfg = ORACLE

    def __init__(self, case: EvalCase, schemas: dict[str, dict[str, Any]]) -> None:
        self.case = case
        self.schemas = schemas

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelInvocationResult:
        offered = {t["function"]["name"] for t in tools}
        tool_results = [m for m in messages if m.get("role") == "tool"]
        candidates = [t for t in self.case.expected_tools if t in offered]

        if not tool_results and candidates:
            tool = candidates[0]
            arguments = oracle_arguments(self.case, tool, self.schemas.get(tool))
            return ModelInvocationResult(
                model_id="oracle", success=True,
                intercepted_tool_calls=[InterceptedToolCall(
                    call_id="oracle_1", tool_name=tool, arguments=arguments,
                    raw_arguments_json=json.dumps(arguments, ensure_ascii=False))],
            )

        if tool_results:
            outputs = [str(m.get("content", "")) for m in tool_results]
            prefix = "Nothing matching was found in the directory.\n" if all(map(_looks_empty, outputs)) else ""
            content = prefix + "Directory result:\n" + "\n".join(o[:3000] for o in outputs)
        else:
            content = "I could not find a matching entry in the directory (nothing found)."
        if self.case.requires_hotline:
            content += "\nIn an emergency call 112, or the TelefonSeelsorge on 0800 111 0 111."
        return ModelInvocationResult(model_id="oracle", success=True, assistant_content=content)
