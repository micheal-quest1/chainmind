"""Gemini client wrapper: multi-model routing, structured output, tool loops.

Three call shapes, picked per stage on purpose:

  `structured()`  one shot, JSON-schema-constrained. Used on the hot path
                  (triage, planning) because a schema-constrained single call
                  is ~1 round-trip instead of 3-6, and cannot return prose we
                  then have to parse. Latency is the judged metric.

  `tool_loop()`   real multi-turn function calling. Used for the healer and the
                  conversational copilot, where the number of steps is not
                  known up front.

  `vision()`      multimodal: image + text -> structured verdict.

Everything is wrapped by the circuit breaker, and every call has a
deterministic fallback so the pipeline completes with no key at all.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from chainmind.core.config import get_settings
from chainmind.core.reliability import Tracer, resilient

try:
    from google import genai
    from google.genai import types as gtypes

    _GENAI = True
except Exception:  # pragma: no cover
    _GENAI = False

_client: Any = None
_resolved_models: dict[str, str] = {}
# Every model that answered a real generateContent call at boot, in probe order.
# A tier whose own preference chain is entirely dead spills into this set rather
# than being pinned to a model we know does not work.
_working_models: list[str] = []
_tier_notes: dict[str, str] = {}


def client() -> Any:
    global _client
    if _client is None:
        s = get_settings()
        if not (_GENAI and s.gemini_api_key):
            raise RuntimeError("gemini unavailable")
        _client = genai.Client(api_key=s.gemini_api_key)
    return _client


# ------------------------------------------------------------ model resolution
# Ordered preference per tier. Every entry is a model we have actually seen
# answer a generateContent call; the resolver verifies again at boot because
# availability is per-key and changes without notice.
_FALLBACK_CHAINS = {
    "triage": ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash",
               "gemini-3.6-flash", "gemini-3-flash-preview"],
    "planner": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.7-flash",
                "gemini-3.5-flash-lite", "gemini-3-flash-preview"],
    "repair": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.8-flash",
               "gemini-3.5-flash", "gemini-3.1-pro-preview"],
    "vision": ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash",
               "gemini-3-flash-preview"],
    "chat": ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash",
             "gemini-3.8-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"],
}

_PROBE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"ok": {"type": "BOOLEAN"}},
    "required": ["ok"],
}


async def _model_answers(name: str) -> bool:
    """A real one-token generation. The only trustworthy availability test.

    `models.list()` cheerfully lists models that return 404 NOT_FOUND on use
    (every gemini-2.5-* name does this for keys created after the cutover), and
    a model can also be listed but quota-exhausted (429). Both failure modes are
    invisible to the listing and fatal at demo time, so we pay ~1s once at boot
    to find out the truth.
    """
    try:
        cfg = gtypes.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_PROBE_SCHEMA,
            max_output_tokens=64,
        )
        resp = await asyncio.wait_for(
            client().aio.models.generate_content(
                model=name, contents='Return {"ok":true}', config=cfg
            ),
            timeout=12.0,
        )
        return bool((resp.text or "").strip())
    except Exception:
        return False


async def resolve_models() -> dict[str, str]:
    """Pin each tier to a model that is verified working for *this* key.

    Hardcoding model IDs is how demos die. Trusting `models.list()` is how they
    die more subtly. We probe every distinct candidate concurrently (so the
    whole resolution costs one round trip, not one per model) and assign each
    tier the first verified entry in its preference chain.
    """
    global _resolved_models, _working_models
    s = get_settings()
    configured = s.models.as_dict()
    if not s.llm_live:
        _resolved_models = {k: f"mock:{v}" for k, v in configured.items()}
        return _resolved_models

    chains = {
        tier: [configured[tier]] + [c for c in _FALLBACK_CHAINS.get(tier, []) if c != configured[tier]]
        for tier in configured
    }
    distinct = sorted({m for chain in chains.values() for m in chain})

    try:
        verdicts = await asyncio.gather(*[_model_answers(m) for m in distinct])
        working = {m for m, ok in zip(distinct, verdicts) if ok}
    except Exception:
        working = set()

    _working_models = [m for m in distinct if m in working]
    _tier_notes.clear()

    out: dict[str, str] = {}
    for tier, chain in chains.items():
        pick = next((c for c in chain if c in working), None)
        if pick is None and _working_models:
            # This tier's whole preference chain is dead for this key (404 for a
            # model that does not exist, or 429 for one that is out of quota).
            # Pinning it to the configured name anyway is how a judged capability
            # ends up permanently "offline" while four other models answer fine,
            # so spill the tier onto a model we just watched work.
            pick = _working_models[0]
            _tier_notes[tier] = (
                f"preference chain unavailable ({', '.join(chain[:3])}); "
                f"spilled to verified model {pick}"
            )
        # Nothing verified at all (network down at boot): keep the configured name
        # and let the per-call circuit breaker handle it rather than refusing to start.
        out[tier] = pick or configured[tier]
        if pick is None:
            _tier_notes[tier] = "no model verified at boot; using configured name unchecked"
    _resolved_models = out
    return out


def resolution_report() -> dict[str, Any]:
    return {
        "resolved": dict(_resolved_models),
        "configured": get_settings().models.as_dict(),
        "verified_working": list(_working_models),
        "tier_notes": dict(_tier_notes),
    }


def model_for(tier: str) -> str:
    if _resolved_models:
        return _resolved_models.get(tier, get_settings().models.as_dict().get(tier, "gemini-2.5-flash"))
    return get_settings().models.as_dict().get(tier, "gemini-2.5-flash")


# ---------------------------------------------------------------- model pool
# The Gemini free tier allows ~5 requests/minute *per model*. A single agent
# conversation can exceed that on its own, and a 429 is indistinguishable from
# an outage to the caller. So instead of retrying the same exhausted model, we
# cool it down for exactly as long as the API asks and spill the request to the
# next model in that tier's preference chain. Multi-model routing stops being a
# nice-to-have and becomes the thing that keeps the system up under load.
_cooldown: dict[str, float] = {}
_RETRY_RE = re.compile(r"retry in ([0-9.]+)s", re.I)


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc)
    return "RESOURCE_EXHAUSTED" in msg or "429" in msg[:40]


def _is_unavailable(exc: Exception) -> bool:
    msg = str(exc)
    return "UNAVAILABLE" in msg or "503" in msg[:40] or "NOT_FOUND" in msg or "404" in msg[:40]


def _retry_after_s(exc: Exception) -> float:
    """Honour the API's own retry hint, capped at one hour.

    The free tier enforces both a per-minute and a per-DAY cap, and the daily
    one comes back as "retry in 16h39m". Capping that at a couple of minutes
    (as a naive backoff would) means hammering a model that is finished for the
    day. Holding it out of rotation for an hour effectively removes it from the
    pool for the session and sends traffic to models that still have budget.
    """
    m = _RETRY_RE.search(str(exc))
    if m:
        try:
            return min(3600.0, float(m.group(1)) + 1.0)
        except ValueError:
            pass
    # Some 429s carry no hint; assume the per-minute window.
    return 65.0


def _cool(model: str, seconds: float) -> None:
    _cooldown[model] = time.monotonic() + seconds


def _is_cool(model: str) -> bool:
    return _cooldown.get(model, 0.0) > time.monotonic()


def candidates_for(tier: str) -> list[str]:
    """Models to try for this tier, quota-available ones first.

    The tier's own preference chain comes first, then every other model that was
    verified working at boot. Without that tail, a tier whose chain is entirely
    rate-limited has nowhere to go and degrades to its deterministic fallback
    even though the key can still reach four other models.
    """
    primary = model_for(tier)
    chain = (
        [primary]
        + [m for m in _FALLBACK_CHAINS.get(tier, []) if m != primary]
        + list(_working_models)
    )
    seen: list[str] = []
    for m in chain:
        if m not in seen:
            seen.append(m)
    ready = [m for m in seen if not _is_cool(m)]
    # Everything cooling? Still try, least-recently-cooled first - better to make
    # one doomed call than to fail the order outright.
    return ready or sorted(seen, key=lambda m: _cooldown.get(m, 0.0))


def pool_report() -> dict[str, Any]:
    now = time.monotonic()
    return {
        m: {"cooling_for_s": round(max(0.0, t - now), 1)}
        for m, t in sorted(_cooldown.items()) if t > now
    }


# ------------------------------------------------------------ thinking control
# Reasoning depth is the single largest latency knob on these models, and the
# API for it changed between families: 2.x takes `thinking_budget` (int), 3.x
# takes `thinking_level` (enum) and hard-400s on the old field. Rather than
# hardcode a mapping that will rot, each model starts on its family's expected
# syntax and silently downgrades the first time the API rejects it.
_THINKING_ORDER = ["level", "budget", "none"]
_thinking_mode: dict[str, str] = {}


def _default_thinking(model: str) -> str:
    return "level" if model.startswith("gemini-3") else "budget"


def _thinking_kwargs(model: str) -> dict[str, Any]:
    mode = _thinking_mode.setdefault(model, _default_thinking(model))
    try:
        if mode == "level":
            return {"thinking_config": gtypes.ThinkingConfig(thinking_level="low")}
        if mode == "budget":
            return {"thinking_config": gtypes.ThinkingConfig(thinking_budget=0)}
    except Exception:
        _thinking_mode[model] = "none"
    return {}


def _downgrade_thinking(model: str) -> bool:
    """Step this model down the ladder. Returns False once at the bottom."""
    cur = _thinking_mode.get(model, _default_thinking(model))
    try:
        nxt = _THINKING_ORDER[_THINKING_ORDER.index(cur) + 1]
    except (ValueError, IndexError):
        return False
    _thinking_mode[model] = nxt
    return True


def _is_bad_argument(exc: Exception) -> bool:
    msg = str(exc)
    return "INVALID_ARGUMENT" in msg or "400" in msg[:40]


def thinking_report() -> dict[str, str]:
    return dict(_thinking_mode)


# -------------------------------------------------------------------- results
@dataclass
class LLMResult:
    data: Any
    model: str
    live: bool
    latency_ms: float
    text: str = ""
    turns: int = 1
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    note: str = ""


# ----------------------------------------------------------------- structured
async def structured(
    tier: str,
    system: str,
    prompt: str,
    schema: dict[str, Any],
    *,
    tracer: Tracer | None = None,
    span_name: str | None = None,
    fallback: Callable[[], Any] | None = None,
    temperature: float = 0.0,
    thinking_budget: int = 0,
) -> LLMResult:
    """One schema-constrained call. `fallback` returns the same shape offline."""
    s = get_settings()
    model = model_for(tier)
    name = span_name or f"llm.{tier}"
    t0 = time.perf_counter()

    if not s.llm_live:
        data = fallback() if fallback else {}
        res = LLMResult(data, f"mock:{model}", False, (time.perf_counter() - t0) * 1000,
                        degraded=True, note="no GEMINI_API_KEY - deterministic fallback")
        if tracer:
            with tracer.measure(name, "agent", model=res.model) as sp:
                sp.attrs.update({"mode": "mock"})
        return res

    cfg_kwargs: dict[str, Any] = {
        "system_instruction": system,
        "temperature": temperature,
        "response_mime_type": "application/json",
        "response_schema": schema,
        "max_output_tokens": 2048,
        "candidate_count": 1,
    }
    used = {"model": model}

    async def call() -> Any:
        last: Exception | None = None
        for candidate in candidates_for(tier)[:3]:
            # Minimum reasoning depth on the hot path, downgrading the syntax if
            # the model rejects it, so one bad field never costs us the request.
            for _ in range(len(_THINKING_ORDER)):
                cfg = gtypes.GenerateContentConfig(**cfg_kwargs, **_thinking_kwargs(candidate))
                try:
                    resp = await client().aio.models.generate_content(
                        model=candidate, contents=prompt, config=cfg
                    )
                except Exception as exc:
                    last = exc
                    if _is_bad_argument(exc) and _downgrade_thinking(candidate):
                        continue
                    if _is_rate_limited(exc):
                        _cool(candidate, _retry_after_s(exc))
                    elif _is_unavailable(exc):
                        _cool(candidate, 60.0)
                    else:
                        raise
                    break  # next candidate model
                raw = (resp.text or "").strip()
                if not raw:
                    last = RuntimeError("empty response")
                    break
                used["model"] = candidate
                return json.loads(raw)
        raise last if last else RuntimeError("no model available")

    async def fb() -> Any:
        return {"__degraded__": True, "data": fallback() if fallback else {}}

    span = tracer.measure(name, "agent", model=model) if tracer else None
    if span:
        span.__enter__()
    try:
        data = await resilient(f"llm.{tier}", call, fallback=fb, timeout_s=s.llm_timeout_s)
        degraded = isinstance(data, dict) and data.get("__degraded__") is True
        if degraded:
            data = data.get("data", {})
        res = LLMResult(data, used["model"], not degraded, (time.perf_counter() - t0) * 1000,
                        degraded=degraded,
                        note="gemini unreachable - deterministic fallback" if degraded else "")
        if span:
            span.attrs.update({"degraded": degraded, "tier": tier,
                               "model_used": used["model"],
                               "failed_over": used["model"] != model})
        return res
    finally:
        if span:
            span.__exit__(None, None, None)


# ------------------------------------------------------------------ tool loop
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def declaration(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


async def tool_loop(
    tier: str,
    system: str,
    prompt: str,
    tools: list[Tool],
    *,
    tracer: Tracer | None = None,
    span_name: str | None = None,
    max_turns: int | None = None,
    temperature: float = 0.1,
    fallback_text: str = "",
) -> LLMResult:
    """Multi-turn function calling with **parallel** tool execution per turn.

    Gemini can emit several function calls in one turn; we run them with
    asyncio.gather instead of sequentially, which is where the wall-clock win
    comes from on the exception path.
    """
    s = get_settings()
    model = model_for(tier)
    name = span_name or f"llm.{tier}.loop"
    max_turns = max_turns or s.max_agent_turns
    t0 = time.perf_counter()
    registry = {t.name: t for t in tools}
    calls_made: list[dict[str, Any]] = []

    if not s.llm_live:
        return LLMResult({}, f"mock:{model}", False, (time.perf_counter() - t0) * 1000,
                         text=fallback_text, degraded=True, note="no GEMINI_API_KEY")

    span = tracer.measure(name, "agent", model=model) if tracer else None
    if span:
        span.__enter__()

    try:
        contents: list[Any] = [
            gtypes.Content(role="user", parts=[gtypes.Part(text=prompt)])
        ]
        active = {"model": model}

        def build_cfg(for_model: str) -> Any:
            return gtypes.GenerateContentConfig(
                system_instruction=system,
                temperature=temperature,
                tools=[gtypes.Tool(function_declarations=[t.declaration() for t in tools])],
                # We drive the loop ourselves so every tool call is traced and retried.
                automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(disable=True),
                max_output_tokens=4096,
                **_thinking_kwargs(for_model),
            )

        final_text = ""
        turns = 0
        for turns in range(1, max_turns + 1):

            async def call() -> Any:
                last: Exception | None = None
                for candidate in candidates_for(tier)[:3]:
                    for _ in range(len(_THINKING_ORDER)):
                        try:
                            resp = await client().aio.models.generate_content(
                                model=candidate, contents=contents, config=build_cfg(candidate)
                            )
                        except Exception as exc:
                            last = exc
                            if _is_bad_argument(exc) and _downgrade_thinking(candidate):
                                continue
                            if _is_rate_limited(exc):
                                _cool(candidate, _retry_after_s(exc))
                            elif _is_unavailable(exc):
                                _cool(candidate, 60.0)
                            else:
                                raise
                            break
                        active["model"] = candidate
                        return resp
                raise last if last else RuntimeError("no model available")

            try:
                resp = await resilient(f"llm.{tier}", call, timeout_s=s.llm_timeout_s)
            except Exception as exc:
                return LLMResult({}, model, False, (time.perf_counter() - t0) * 1000,
                                 text=fallback_text or f"agent unavailable: {exc}", turns=turns,
                                 tool_calls=calls_made, degraded=True, note=str(exc))

            cand = (resp.candidates or [None])[0]
            parts = list(getattr(getattr(cand, "content", None), "parts", None) or [])
            fcalls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            # Only count visible answer text: 3.x models emit reasoning parts whose
            # `thought` flag is set, and those must not be shown to the user.
            text_bits = [
                p.text for p in parts
                if getattr(p, "text", None) and not getattr(p, "thought", False)
            ]
            if text_bits:
                final_text = "\n".join(t for t in text_bits if t)
            elif not fcalls:
                # No parts we recognised, but the turn is over - take the SDK's
                # own flattened text rather than returning an empty answer.
                try:
                    final_text = (resp.text or "").strip() or final_text
                except Exception:
                    pass

            if not fcalls:
                break

            contents.append(gtypes.Content(role="model", parts=parts))

            async def run_one(fc: Any) -> gtypes.Part:
                fname = fc.name
                args = dict(fc.args or {})
                tool = registry.get(fname)
                tsp = tracer.measure(f"tool.{fname}", "tool") if tracer else None
                if tsp:
                    tsp.__enter__()
                try:
                    if tool is None:
                        result: Any = {"error": f"unknown tool {fname}"}
                    elif asyncio.iscoroutinefunction(tool.handler):
                        result = await tool.handler(**args)
                    else:
                        result = await asyncio.to_thread(lambda: tool.handler(**args))
                except Exception as exc:  # tool errors are data, not crashes:
                    result = {"error": type(exc).__name__, "detail": str(exc)}
                finally:
                    if tsp:
                        tsp.attrs.update({"args": args})
                        tsp.__exit__(None, None, None)
                calls_made.append({"tool": fname, "args": args})
                if not isinstance(result, dict):
                    result = {"result": result}
                return gtypes.Part(
                    function_response=gtypes.FunctionResponse(name=fname, response=result)
                )

            responses = await asyncio.gather(*[run_one(fc) for fc in fcalls])
            contents.append(gtypes.Content(role="user", parts=list(responses)))

        if span:
            span.attrs.update({"turns": turns, "tool_calls": len(calls_made),
                               "model_used": active["model"]})
        return LLMResult({}, active["model"], True, (time.perf_counter() - t0) * 1000,
                         text=final_text, turns=turns, tool_calls=calls_made)
    finally:
        if span:
            span.__exit__(None, None, None)


# -------------------------------------------------------------------- vision
async def vision(
    system: str,
    prompt: str,
    image_bytes: bytes,
    mime_type: str,
    schema: dict[str, Any],
    *,
    tracer: Tracer | None = None,
    fallback: Callable[[], Any] | None = None,
) -> LLMResult:
    s = get_settings()
    model = model_for("vision")
    t0 = time.perf_counter()

    if not s.llm_live:
        return LLMResult(fallback() if fallback else {}, f"mock:{model}", False,
                         (time.perf_counter() - t0) * 1000, degraded=True,
                         note="no GEMINI_API_KEY")

    async def call() -> Any:
        cfg = gtypes.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=1024,
        )
        resp = await client().aio.models.generate_content(
            model=model,
            contents=[
                gtypes.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                gtypes.Part(text=prompt),
            ],
            config=cfg,
        )
        return json.loads((resp.text or "").strip())

    async def fb() -> Any:
        return {"__degraded__": True, "data": fallback() if fallback else {}}

    span = tracer.measure("llm.vision", "agent", model=model) if tracer else None
    if span:
        span.__enter__()
    try:
        data = await resilient("llm.vision", call, fallback=fb, timeout_s=s.llm_timeout_s)
        degraded = isinstance(data, dict) and data.get("__degraded__") is True
        if degraded:
            data = data.get("data", {})
        return LLMResult(data, model, not degraded, (time.perf_counter() - t0) * 1000,
                         degraded=degraded)
    finally:
        if span:
            span.__exit__(None, None, None)
