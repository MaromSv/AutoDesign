"""Shared client for NVIDIA Nemotron models hosted on Nebius Token Factory.

Nebius Token Factory exposes an OpenAI-compatible Chat Completions API, so we drive it
through the `openai` SDK pointed at Nebius' base URL. Two signals use this:

  - `stress_test`        — judges whether the rendered site's interactions actually work.
  - `prompt_consistency` — judges whether the generated site matches the brief (the prompt).

Everything is config-driven from the `nemotron:` block in `autodesign.md` (base URL, model,
which env var holds the key). Each consumer signal may override the model via its own block.

This module never raises on missing deps/keys to the caller's surprise: it raises the
typed `NemotronUnavailable`, which signals catch and turn into a graceful `score=None` skip.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

# Nebius Token Factory's OpenAI-compatible endpoint. Overridable via `nemotron.base_url`.
DEFAULT_BASE_URL = "https://api.tokenfactory.us-central1.nebius.com/v1/"
# A capable Nemotron tier verified on Token Factory (returns clean JSON for our signals).
# Overridable via `nemotron.model` (or a signal's own `model:`). Pin whatever Nemotron id
# you have access to in your Token Factory account.
DEFAULT_MODEL = "nvidia/Nemotron-3-Ultra-550b-a55b"
# Where the Nebius key lives. Configurable so it can sit alongside other provider keys.
DEFAULT_KEY_ENV = "NEBIUS_API_KEY"
DEFAULT_TEMPERATURE = 0.2
# Nemotron is a reasoning model: by default it emits a long thinking trace before the
# answer, which eats the token budget (truncating our JSON) for no benefit on a
# structured-scoring task. Llama-Nemotron toggles the trace via a system directive
# ("detailed thinking on"/"off"). We default OFF so the model returns the answer directly.
# Override per run via `nemotron.reasoning: on` (or a signal's own block).
DEFAULT_REASONING = "off"


def _resolve_reasoning(config: dict | None, override: str | None = None) -> str:
    """Pick the reasoning mode: explicit override > nemotron.reasoning > default ('off')."""
    if override is not None:
        return str(override).strip().lower()
    nm = nemotron_config(config)
    return str(nm.get("reasoning") or DEFAULT_REASONING).strip().lower()


def _apply_reasoning(messages: list[dict], reasoning: str) -> list[dict]:
    """Prepend the Llama-Nemotron `detailed thinking on|off` system directive.

    Folds into an existing leading system message when present (string content),
    otherwise inserts a dedicated system message at the front. Unknown modes are a
    no-op so a bad config value can't break the call.
    """
    if reasoning not in ("on", "off"):
        return messages
    directive = f"detailed thinking {reasoning}"
    msgs = [dict(m) for m in messages]
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        msgs[0]["content"] = f"{directive}\n\n{msgs[0]['content']}"
        return msgs
    return [{"role": "system", "content": directive}, *msgs]


class NemotronUnavailable(RuntimeError):
    """Raised when Nemotron cannot run for an expected reason (no SDK / no key)."""


def nemotron_config(config: dict | None) -> dict:
    """Return the `nemotron:` block from the parsed autodesign config (or {})."""
    return ((config or {}).get("nemotron") or {})


def resolve_model(config: dict | None, override: str | None = None) -> str:
    """Pick the Nemotron model id: signal override > nemotron.model > default."""
    if override:
        return str(override)
    nm = nemotron_config(config)
    return str(nm.get("model") or DEFAULT_MODEL)


def _client(config: dict | None):
    """Build an OpenAI SDK client pointed at Nebius. Raises NemotronUnavailable.

    Key check comes before the SDK import: a missing key is the most common, most
    actionable setup error, and reporting it doesn't require the SDK to be present.
    """
    # Pick up AutoDesign/.env so the key works without `export`, regardless of which
    # entry point invoked the signal (rank/batch load it too; this covers the rest).
    from pipeline.envfile import ensure_loaded
    ensure_loaded()

    nm = nemotron_config(config)
    key_env = str(nm.get("api_key_env") or DEFAULT_KEY_ENV)
    api_key = os.getenv(key_env)
    if not api_key:
        raise NemotronUnavailable(f"{key_env} not set (Nebius Token Factory API key)")

    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - exercised only without the SDK
        raise NemotronUnavailable("openai SDK not installed (pip install openai)") from exc

    base_url = str(nm.get("base_url") or DEFAULT_BASE_URL)
    return OpenAI(api_key=api_key, base_url=base_url)


def available(config: dict | None) -> bool:
    """True if a Nemotron call could be made (key present + SDK importable)."""
    try:
        _client(config)
        return True
    except NemotronUnavailable:
        return False


def image_content(path: str | Path) -> dict:
    """OpenAI-style image content part (data URL). For vision-capable Nemotron tiers."""
    p = Path(path)
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    ext = p.suffix.lower().lstrip(".")
    media = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
    return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}


def chat(
    config: dict | None,
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 2048,
    temperature: float | None = None,
    force_json: bool = False,
    reasoning: str | None = None,
) -> str:
    """Call Nemotron via Nebius and return the assistant's text.

    `force_json` asks for `response_format={"type": "json_object"}`; if the server
    rejects it (some OpenAI-compatible backends do), we retry once without it — the
    prompt should still instruct the model to emit JSON, and `extract_json` is lenient.

    `reasoning` ('on'/'off', default from config, default 'off') toggles the model's
    thinking trace — off makes it answer directly, which is what our JSON signals want.
    """
    client = _client(config)
    resolved = resolve_model(config, model)
    temp = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
    messages = _apply_reasoning(messages, _resolve_reasoning(config, reasoning))

    kwargs: dict = {
        "model": resolved,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    if force_json:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if force_json and _looks_like_response_format_error(exc):
            kwargs.pop("response_format", None)
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    return resp.choices[0].message.content or ""


def chat_raw(
    config: dict | None,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float | None = None,
    reasoning: str | None = None,
):
    """Call Nemotron and return the raw assistant `message` object (SDK type).

    Unlike `chat`, this exposes `message.tool_calls` so callers can drive a tool-use
    loop (the agentic stress test). `tools`/`tool_choice` are the standard OpenAI
    function-calling params; omit `tools` for a plain completion. `reasoning` toggles
    the thinking trace (default off via config).
    """
    client = _client(config)
    resolved = resolve_model(config, model)
    temp = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
    messages = _apply_reasoning(messages, _resolve_reasoning(config, reasoning))

    kwargs: dict = {
        "model": resolved,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temp,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message


def _looks_like_response_format_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "response_format" in msg or "json_object" in msg


def extract_json(text: str) -> dict | None:
    """Best-effort: pull the first JSON object out of a model response.

    Tolerant of ```json fences and leading/trailing prose, mirroring the parsing the
    vlm_judge signal does for Claude/OpenAI responses.
    """
    if not text:
        return None
    candidates = [text]
    fence = _strip_fence(text)
    if fence:
        candidates.append(fence)
    block = _first_brace_block(text)
    if block:
        candidates.append(block)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _strip_fence(text: str) -> str | None:
    t = text.strip()
    if "```" not in t:
        return None
    inner = t.split("```", 2)
    if len(inner) < 2:
        return None
    body = inner[1]
    if body.lstrip().lower().startswith("json"):
        body = body.lstrip()[4:]
    return body.strip()


def _first_brace_block(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]
