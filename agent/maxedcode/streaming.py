"""One model request read as a stream (headless loop and subagents share it)."""
import time
from contextlib import suppress
from types import SimpleNamespace

from . import llmretry
from .util import think_loop

STREAM_USAGE = [True]    # stream_options.include_usage; off once a server rejects the field


class OneChunkStream:
    """A complete (non-streamed) response read through the stream interface: one chunk with the whole
    message, then one with the usage. Lets a stream consumer (the TUI loop) send plain requests unchanged."""

    def __init__(self, response):
        self.response = response

    def __aiter__(self):
        return self._chunks()

    async def _chunks(self):
        res = self.response
        if res.choices:
            choice, message = res.choices[0], res.choices[0].message
            extra = dict(getattr(message, "model_extra", None) or {})
            reasoning = extra.get("reasoning_content") or extra.get("reasoning") or getattr(message, "reasoning_content", None)
            calls = [SimpleNamespace(index=i, id=tc.id, function=SimpleNamespace(name=tc.function.name,
                                                                                 arguments=tc.function.arguments))
                     for i, tc in enumerate(getattr(message, "tool_calls", None) or [])]
            delta = SimpleNamespace(content=message.content, tool_calls=calls or None, reasoning_content=reasoning,
                                    model_extra={"reasoning_content": reasoning} if reasoning else {})
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=choice.finish_reason)], usage=None)
        if getattr(res, "usage", None):
            yield SimpleNamespace(choices=[], usage=res.usage)

    async def aclose(self):
        pass

    async def close(self):
        pass


async def stream_completion(client, on_delta=None, **kw):
    """One model request read as a stream, returned in the shape of a non-streamed response.

    Streaming is how the TUI stops a reasoning that has fallen into a loop after ~100 tokens. Without it
    headless waited for the whole reply: live X5 spent 60-150 s per step on ~8200 reasoning tokens, one
    of them "Enough! Let me consider Argan? no." over and over. Returns (response, looped); a looped
    reply is cut, and the caller repeats the step without reasoning instead of keeping it.
    on_delta(reasoning, content) — optional, called with the text so far as it grows (live views).
    A client that answers with a complete response (tests, a server without streaming) is used as is."""
    if STREAM_USAGE[0]:
        kw["stream_options"] = {"include_usage": True}
    t0 = time.monotonic()
    res = await client.chat.completions.create(stream=True, **kw)
    if hasattr(res, "choices"):
        return res, False
    content, reasoning, calls, usage, finish, looped, checked = "", "", {}, None, None, False, 0
    first, timings = None, None   # time to the first token (prompt read), the server's own timings (llama.cpp)
    try:
        async for ch in res:
            if getattr(ch, "usage", None):
                usage = ch.usage
            timings = (getattr(ch, "model_extra", None) or {}).get("timings") or timings
            if not ch.choices:
                continue
            if first is None:
                first = time.monotonic() - t0
            finish = ch.choices[0].finish_reason or finish
            d = ch.choices[0].delta
            r = (getattr(d, "model_extra", None) or {}).get("reasoning_content") or \
                (getattr(d, "model_extra", None) or {}).get("reasoning") or getattr(d, "reasoning_content", None)
            if r:
                reasoning += r
                if not content and not calls and len(reasoning) - checked >= 64:
                    checked = len(reasoning)
                    if think_loop(reasoning):
                        looped = True
                        break
            if d.content:
                content += d.content
            for tc in d.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function:
                    c["name"] += tc.function.name or ""
                    c["args"] += tc.function.arguments or ""
            if on_delta and (r or d.content):
                with suppress(Exception):     # a broken live view never stops the request
                    on_delta(reasoning, content)
    finally:
        if looped:
            for close in ("close", "aclose"):
                with suppress(Exception):
                    await getattr(res, close)()   # stop generation on the server too
                    break
    if not looped:
        llmretry.stream_ended(finish)   # a cut stream is a failure to retry, not a short answer
    tool_calls = [SimpleNamespace(id=c["id"] or f"call_{i}", type="function",
                                  function=SimpleNamespace(name=c["name"], arguments=c["args"] or "{}"))
                  for i, c in sorted(calls.items())]
    message = SimpleNamespace(content=content, tool_calls=tool_calls or None, reasoning_content=reasoning or None,
                              model_extra={"reasoning_content": reasoning} if reasoning else {})
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage,
                           timings=timings, first_ms=int(first * 1000) if first is not None else None), looped
