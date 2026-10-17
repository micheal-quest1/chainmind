"""Self-healing primitives: tracing spans, retry-with-jitter, circuit breakers.

Every external dependency in ChainMind (Gemini, Google Routes, carrier APIs) is
wrapped by `resilient()`. A dependency that starts failing is tripped open after
N consecutive failures, which makes the pipeline *degrade* to a deterministic
fallback instead of stalling. That is the difference between "it crashed" and
"it shipped the order anyway and told you why".
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from chainmind.core.config import get_settings

T = TypeVar("T")


# --------------------------------------------------------------------- spans
@dataclass
class Span:
    name: str
    kind: str
    started_at: float
    duration_ms: float
    ok: bool
    model: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 2),
            "ok": self.ok,
            "model": self.model,
            "attrs": self.attrs,
        }


class Tracer:
    """Collects spans for one pipeline run; also mirrors them into SQLite."""

    def __init__(self, order_id: str | None = None) -> None:
        self.order_id = order_id
        self.spans: list[Span] = []
        self._t0 = time.perf_counter()

    def add(self, span: Span) -> None:
        self.spans.append(span)
        try:
            from chainmind.core.db import get_db

            get_db().record_span(
                span.name,
                span.kind,
                span.started_at,
                span.duration_ms,
                ok=span.ok,
                order_id=self.order_id,
                model=span.model,
                attrs=span.attrs,
            )
        except Exception:
            pass  # tracing must never break the flow

    def measure(self, name: str, kind: str, *, model: str | None = None) -> "_SpanCtx":
        return _SpanCtx(self, name, kind, model)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, float] = {}
        for s in self.spans:
            by_kind[s.kind] = by_kind.get(s.kind, 0.0) + s.duration_ms
        return {
            "total_ms": round(self.elapsed_ms, 2),
            "span_count": len(self.spans),
            "ms_by_kind": {k: round(v, 2) for k, v in by_kind.items()},
            "failures": sum(0 if s.ok else 1 for s in self.spans),
        }


class _SpanCtx:
    def __init__(self, tracer: Tracer, name: str, kind: str, model: str | None) -> None:
        self.tracer, self.name, self.kind, self.model = tracer, name, kind, model
        self.attrs: dict[str, Any] = {}

    def __enter__(self) -> "_SpanCtx":
        self._start_wall = time.time()
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.tracer.add(
            Span(
                name=self.name,
                kind=self.kind,
                started_at=self._start_wall,
                duration_ms=(time.perf_counter() - self._start) * 1000,
                ok=exc_type is None,
                model=self.model,
                attrs=self.attrs,
            )
        )
        return False

    async def __aenter__(self) -> "_SpanCtx":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self.__exit__(exc_type, exc, tb)


# ----------------------------------------------------------- circuit breaker
class BreakerOpen(RuntimeError):
    """Raised when a dependency is tripped open; callers fall back."""


@dataclass
class CircuitBreaker:
    name: str
    threshold: int
    cooldown_s: float
    failures: int = 0
    opened_at: float | None = None
    half_open: bool = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            return "half-open"
        return "open"

    def guard(self) -> None:
        st = self.state
        if st == "open":
            raise BreakerOpen(f"{self.name} circuit open ({self.failures} consecutive failures)")
        if st == "half-open":
            self.half_open = True

    def on_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self.half_open = False

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        return {"name": self.name, "state": self.state, "failures": self.failures}


_breakers: dict[str, CircuitBreaker] = {}


def breaker(name: str) -> CircuitBreaker:
    s = get_settings()
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name, s.breaker_threshold, s.breaker_cooldown_s)
    return _breakers[name]


def breaker_snapshot() -> list[dict[str, Any]]:
    return [b.snapshot() for b in _breakers.values()]


# -------------------------------------------------------------------- retry
async def resilient(
    name: str,
    fn: Callable[[], Awaitable[T]],
    *,
    fallback: Callable[[], Awaitable[T]] | None = None,
    attempts: int | None = None,
    timeout_s: float | None = None,
) -> T:
    """Run `fn` under a circuit breaker with exponential backoff + jitter.

    On exhaustion, `fallback` runs (degraded mode). If there is no fallback the
    error propagates so the healer agent can take over.
    """
    s = get_settings()
    attempts = attempts or s.tool_max_attempts
    cb = breaker(name)

    try:
        cb.guard()
    except BreakerOpen:
        if fallback is not None:
            return await fallback()
        raise

    last: Exception | None = None
    for i in range(attempts):
        try:
            if timeout_s:
                result = await asyncio.wait_for(fn(), timeout=timeout_s)
            else:
                result = await fn()
            cb.on_success()
            return result
        except Exception as exc:  # noqa: BLE001 - deliberate: any failure retries
            last = exc
            cb.on_failure()
            if i < attempts - 1:
                base = s.tool_backoff_base_ms * (2**i)
                await asyncio.sleep((base + random.uniform(0, base * 0.5)) / 1000)

    if fallback is not None:
        return await fallback()
    raise last if last else RuntimeError(f"{name} failed")
