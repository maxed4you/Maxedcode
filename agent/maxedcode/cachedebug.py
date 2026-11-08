"""Prefix cache diagnostics: where a request differs from the previous one.

Enabled by AGENT_CACHE_DEBUG=1 (or "cache_debug": true in settings.json). Before every request
the UI shows a line in the chat, headless writes it to stderr; both also append it to
HOME/cache_debug.log. After the response the server-reported cached_tokens is shown too. If a request
only appends to the history, the KV cache should reuse the matching prefix; otherwise the line names
the first difference that may invalidate that prefix.
"""
import json
import time

from .config import HOME

_prev = {"msgs": None, "tools": None, "tmpl": None, "choice": None,
         "key": None, "pin": None, "cache_prompt": None}
_n = 0


def _j(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)


def _field_diff(a, b):
    """Which fields of two messages differ."""
    try:
        x, y = json.loads(a), json.loads(b)
    except ValueError:
        return "content"
    keys = sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))
    return ",".join(keys) or "?"


def report(messages, tools, extra=None, tool_choice=None):
    """Comparison line against the previous request; remembers the current one as "previous"."""
    global _n
    _n += 1
    msgs = [_j(m) for m in messages]
    cur = {"msgs": msgs, "tools": _j(tools), "tmpl": _j((extra or {}).get("chat_template_kwargs")),
           "choice": tool_choice or "auto", "key": (extra or {}).get("promptCacheKey"),
           "pin": _j((extra or {}).get("strata_prefix")),
           "cache_prompt": (extra or {}).get("cache_prompt")}
    if _prev["msgs"] is None:
        line = f"cache-debug #{_n}: first request ({len(msgs)} msgs, {len(tools or [])} tools)"
    else:
        old = _prev["msgs"]
        common = 0
        while common < min(len(old), len(msgs)) and old[common] == msgs[common]:
            common += 1
        parts = [
            "tools " + ("same" if cur["tools"] == _prev["tools"] else "CHANGED (prefix invalidation)"),
            "template_kwargs " + ("same" if cur["tmpl"] == _prev["tmpl"] else "CHANGED (rendering may invalidate prefix)"),
            "tool_choice " + (cur["choice"] if cur["choice"] == _prev["choice"] else f"{_prev['choice']}->{cur['choice']} CHANGED"),
            "cache_key " + ("same" if cur["key"] == _prev["key"] else f"{_prev['key']}->{cur['key']} CHANGED"),
            "strata_prefix " + ("same" if cur["pin"] == _prev["pin"] else "CHANGED"),
            "cache_prompt " + ("same" if cur["cache_prompt"] == _prev["cache_prompt"] else "CHANGED"),
        ]
        if common == len(old):
            parts.append(f"msgs: append-only OK ({len(old)} -> {len(msgs)})")
        else:
            role = json.loads(msgs[common]).get("role") if common < len(msgs) else "?"
            what = _field_diff(old[common], msgs[common]) if common < len(msgs) else "removed"
            parts.append(f"msgs: first difference at #{common} ({role}, fields: {what}); "
                         f"{common} of {len(old)} previous msgs reusable")
        line = f"cache-debug #{_n}: " + " | ".join(parts)
    _prev.update(cur)
    try:
        with open(HOME / "cache_debug.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + line + "\n")
    except OSError:
        pass
    return line

