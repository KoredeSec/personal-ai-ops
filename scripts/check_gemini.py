"""Live Gemini check: one plain call and one structured-JSON call on the light model.

    python -m scripts.check_gemini
"""

import asyncio
import sys

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.llm import GeminiClient, LLMError
from app.core.logging import configure_logging


class Probe(BaseModel):
    ok: bool
    capital_of_nigeria: str


async def run() -> int:
    s = get_settings()
    configure_logging("INFO", s.secret_values())
    client = GeminiClient(s)  # no store: does not touch the database
    try:
        r = await client.generate("Reply with exactly: OK", tier="light", temperature=0, max_output_tokens=256,
                                  action="check")
        print(f"OK text: model={r.model} reply={r.text.strip()!r} tokens={r.total_tokens} {r.latency_ms}ms")
        p = await client.generate_json("Return ok=true and the capital of Nigeria.", Probe, tier="light",
                                       temperature=0, max_output_tokens=512, action="check_json")
        print(f"OK json: {p.model_dump()}")
    except LLMError as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}\nUser-facing message: {exc.user_message}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
