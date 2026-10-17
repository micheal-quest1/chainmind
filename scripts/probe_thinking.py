"""Find the thinking-control syntax each model family accepts.

Gemini 2.x used `thinking_budget` (an int). Gemini 3.x replaced it with
`thinking_level`. Sending the wrong one is a hard 400, so the runtime needs to
know which to use - and this is how we find out instead of guessing.
"""
from __future__ import annotations
import asyncio, sys, time
sys.path.insert(0, ".")
from chainmind.core.config import get_settings
from google import genai
from google.genai import types as g

SCHEMA = {"type": "OBJECT", "properties": {"word": {"type": "STRING"}}, "required": ["word"]}
VARIANTS = {
    "none": None,
    "level_low": {"thinking_level": "low"},
    "level_LOW": {"thinking_level": "LOW"},
    "budget_0": {"thinking_budget": 0},
}


async def run(cl, model: str, label: str, kw) -> None:
    t0 = time.perf_counter()
    try:
        tc = g.ThinkingConfig(**kw) if kw else None
    except Exception as e:
        print(f"  {label:10s} CONFIG-REJECT {type(e).__name__}")
        return
    try:
        r = await cl.aio.models.generate_content(
            model=model,
            contents='Return {"word":"ok"}',
            config=g.GenerateContentConfig(
                temperature=0, response_mime_type="application/json",
                response_schema=SCHEMA, max_output_tokens=256,
                **({"thinking_config": tc} if tc else {}),
            ),
        )
        print(f"  {label:10s} OK   {(time.perf_counter()-t0)*1000:6.0f}ms  {(r.text or '')[:30]!r}")
    except Exception as e:
        print(f"  {label:10s} FAIL {(time.perf_counter()-t0)*1000:6.0f}ms  {type(e).__name__}: {str(e)[:110]}")


async def main() -> None:
    cl = genai.Client(api_key=get_settings().gemini_api_key)
    for model in ("gemini-3.5-flash-lite", "gemini-3.5-flash"):
        print(model)
        await asyncio.gather(*[run(cl, model, k, v) for k, v in VARIANTS.items()])


asyncio.run(main())
