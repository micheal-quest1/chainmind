"""Diagnose why a tool-loop turn produced no visible answer."""
from __future__ import annotations
import asyncio, sys
sys.path.insert(0, ".")
from chainmind.core.config import get_settings
from chainmind.agents import copilot, llm
from google import genai
from google.genai import types as g


async def main() -> None:
    await llm.resolve_models()
    model = llm.model_for("chat")
    tools = copilot.copilot_tools()
    cl = genai.Client(api_key=get_settings().gemini_api_key)
    for max_tok in (2048, 8192):
        cfg = g.GenerateContentConfig(
            system_instruction=copilot.COPILOT_SYSTEM,
            temperature=0.1,
            tools=[g.Tool(function_declarations=[t.declaration() for t in tools])],
            automatic_function_calling=g.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=max_tok,
            thinking_config=g.ThinkingConfig(thinking_level="low"),
        )
        r = await cl.aio.models.generate_content(
            model=model, contents="MANAGER: Which warehouses have vaccine kits?", config=cfg)
        c = (r.candidates or [None])[0]
        parts = list(getattr(getattr(c, "content", None), "parts", None) or [])
        print(f"max_output_tokens={max_tok} model={model}")
        print(f"  finish_reason : {getattr(c,'finish_reason',None)}")
        print(f"  usage         : {r.usage_metadata}")
        print(f"  parts         : {len(parts)}")
        for p in parts:
            print(f"    text={str(getattr(p,'text',None))[:60]!r} "
                  f"thought={getattr(p,'thought',None)} "
                  f"fc={getattr(getattr(p,'function_call',None),'name',None)}")
        print()


asyncio.run(main())
