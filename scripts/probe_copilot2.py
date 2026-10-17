import asyncio, sys
sys.path.insert(0, ".")
from chainmind.agents import copilot, llm


async def main():
    await llm.resolve_models()
    r = await copilot.chat("Which warehouses have vaccine kits?")
    print("model          :", r["model"])
    print("turns          :", r["turns"], "tools:", [c["tool"] for c in r["tool_calls"]])
    print("degraded       :", r["degraded"])
    print("degraded_reason:", (r.get("degraded_reason") or "")[:600])
    print("reply[:300]    :", (r["reply"] or "")[:300])


asyncio.run(main())
