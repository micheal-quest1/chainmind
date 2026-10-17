"""Boot-time verification: prove which Google APIs this deployment can reach.

Run this before a demo. It reads keys from the environment and never prints them.
Exit code 0 means every configured API answered; non-zero means at least one
surface is degraded (the app still runs, on fallbacks).
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")


async def main() -> int:
    from chainmind.core.config import get_settings

    s = get_settings()
    print(f"gemini key present : {bool(s.gemini_api_key)}")
    print(f"maps   key present : {bool(s.maps_api_key)}")

    from chainmind.agents import llm
    from chainmind.services import maps

    ok = True
    try:
        models = await llm.resolve_models()
        print("\n[gemini] resolved model tiers:")
        for tier, m in models.items():
            print(f"   {tier:9s} -> {m}")
        if any(m.startswith("mock:") for m in models.values()):
            ok = False
    except Exception as e:
        print(f"[gemini] model listing FAILED: {type(e).__name__}: {e}")
        ok = False

    try:
        res = await llm.structured(
            "triage",
            "You reply only in JSON.",
            "Return the word ok.",
            {"type": "OBJECT", "properties": {"word": {"type": "STRING"}}, "required": ["word"]},
        )
        print(
            f"[gemini] live call  : {res.model} -> {res.data} "
            f"({res.latency_ms:.0f}ms, degraded={res.degraded}) {res.note}"
        )
        ok = ok and not res.degraded
    except Exception as e:
        print(f"[gemini] live call FAILED: {type(e).__name__}: {e}")
        ok = False

    # Raw SDK call with errors surfaced, so a failure names its own cause
    # instead of hiding behind the fallback.
    if "--raw" in sys.argv:
        try:
            from google import genai
            from google.genai import types as g

            cl = genai.Client(api_key=s.gemini_api_key)
            r = await cl.aio.models.generate_content(
                model=llm.model_for("triage"),
                contents="Return the word ok.",
                config=g.GenerateContentConfig(
                    system_instruction="You reply only in JSON.",
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema={
                        "type": "OBJECT",
                        "properties": {"word": {"type": "STRING"}},
                        "required": ["word"],
                    },
                    max_output_tokens=512,
                ),
            )
            print(f"[gemini] raw call   : {r.text!r}")
        except Exception as e:
            print(f"[gemini] raw call FAILED: {type(e).__name__}: {str(e)[:600]}")

    report = await maps.probe_apis()
    print(f"\n[maps] {report}")
    await maps.aclose()

    print("\nRESULT:", "all configured Google APIs reachable" if ok else "DEGRADED (fallbacks active)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
