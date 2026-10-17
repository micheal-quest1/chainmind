"""Probe candidate models with a real generateContent call.

`models.list()` is not a capability check - it happily lists models that 404 on
use. The only reliable test is a one-token generation, so that is what we do.
"""
from __future__ import annotations

import asyncio
import sys
import time

sys.path.insert(0, ".")

from chainmind.core.config import get_settings
from google import genai
from google.genai import types as g

CANDIDATES = [
    "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview",
    "gemini-3.1-pro-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
]


async def probe(cl, name: str) -> tuple[str, bool, float, str]:
    t0 = time.perf_counter()
    try:
        r = await cl.aio.models.generate_content(
            model=name,
            contents="Return JSON {\"word\":\"ok\"}",
            config=g.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema={"type": "OBJECT", "properties": {"word": {"type": "STRING"}},
                                 "required": ["word"]},
                max_output_tokens=256,
            ),
        )
        return name, True, (time.perf_counter() - t0) * 1000, (r.text or "")[:40]
    except Exception as e:
        return name, False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {str(e)[:90]}"


async def main() -> None:
    cl = genai.Client(api_key=get_settings().gemini_api_key)
    results = await asyncio.gather(*[probe(cl, n) for n in CANDIDATES])
    for name, ok, ms, detail in results:
        print(f"{'OK ' if ok else 'FAIL'} {ms:7.0f}ms  {name:28s} {detail}")


asyncio.run(main())
