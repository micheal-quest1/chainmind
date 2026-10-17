"""List the generateContent models this API key can actually use."""
from __future__ import annotations
import sys
sys.path.insert(0, ".")

from chainmind.core.config import get_settings
from google import genai

s = get_settings()
cl = genai.Client(api_key=s.gemini_api_key)
names = []
for m in cl.models.list():
    n = (m.name or "").removeprefix("models/")
    if "generateContent" in (m.supported_actions or []):
        names.append(n)
for n in sorted(names):
    print(" ", n)
print(f"\ntotal: {len(names)}")
