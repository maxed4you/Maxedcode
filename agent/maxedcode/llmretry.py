"""Model requests that survive a server failure: one retry policy for the TUI, headless and subagents.

The OpenAI SDK defaults were in use: 2 quiet retries within ~8 s, and a 600 s read timeout after which the
SDK silently sent the whole request again. A local server (Strata) that crashed and restarted took longer
than 8 s, so a turn ended in "Error: APIConnectionError" and a subagent lost all its steps. DeepSeek Harness
classifies such failures (transport, 5xx, timeout, rate limit, a stream that ended without a finish reason)
and retries them with backoff (`llm-pi-ai/src/stream.ts`, `llm-retry`). The same here:

* the SDK does not retry (max_retries=0); this module does, with the same request bytes, so a server that
  comes back still finds the cached prefix;
* only failures retry: a 400 (the server refused the request) goes to the caller's own handling as before;
* waits 2, 5, 10, 20, 40 s (Retry-After for 429), so a restart of up to ~1.5 min is survived;
* a cancel (Esc) during a wait stops at once.
"""
import asyncio
import os

try:     # both come with the openai package; offline test environments stub openai and have neither
    import httpx
except ImportError:
    httpx = None
try:
    import openai
except ImportError:
    openai = None

RETRIES = int(os.environ.get("AGENT_LLM_RETRIES", "5"))
DELAYS = (2, 5, 10, 20, 40)
# Without streaming the server sends nothing until the reply is complete: a long prefill plus a long
# reasoning on a slow local server can take many minutes. Silence is limited, not the whole request.
READ_TIMEOUT = float(os.environ.get("AGENT_READ_TIMEOUT", "1800"))
FINISH_SEEN = [False]    # this server has ended a stream with a finish reason: a stream without one is a cut


class StreamCut(Exception):
    """The stream ended without a finish reason on a server that normally sends one: the connection broke."""


RETRYABLE = tuple(x for x in (getattr(openai, "APIConnectionError", None), getattr(openai, "InternalServerError", None),
                               getattr(openai, "RateLimitError", None), getattr(httpx, "TransportError", None),
                               StreamCut)
                  if isinstance(x, type))


def client_options():
    """AsyncOpenAI keyword arguments for the local model: no SDK retries (they are done here), long read timeout."""
    timeout = httpx.Timeout(READ_TIMEOUT, connect=10.0) if httpx else READ_TIMEOUT
    return {"max_retries": 0, "timeout": timeout}


def stream_ended(finish):
    """Call when a stream is over: raises StreamCut when it ended without a finish reason after the server
    has shown that it sends one (a server that never sends it is not punished)."""
    if finish:
        FINISH_SEEN[0] = True
    elif FINISH_SEEN[0]:
        raise StreamCut("the stream ended without a finish reason")


def _delay(exc, attempt):
    if type(exc).__name__ == "RateLimitError":
        try:
            return min(60.0, float(exc.response.headers.get("retry-after")))
        except (AttributeError, TypeError, ValueError):
            pass
    return DELAYS[min(attempt, len(DELAYS) - 1)]


def describe(exc):
    return f"{type(exc).__name__}: {str(exc)[:160]}"


async def call(fn, on_retry=None):
    """await fn() and repeat it on a server failure. on_retry(exc, attempt, wait) may be sync or async."""
    for attempt in range(RETRIES + 1):
        try:
            return await fn()
        except RETRYABLE as exc:
            if attempt >= RETRIES:
                raise
            wait = _delay(exc, attempt)
            if on_retry:
                note = on_retry(exc, attempt + 1, wait)
                if asyncio.iscoroutine(note):
                    await note
            await asyncio.sleep(wait)
