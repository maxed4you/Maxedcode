"""Stable prompt-cache key for local inference servers that support it.

The key decides which requests share one prefix-cache slot. Keying by cwd alone makes every session
of the same project share one slot, so a new session (or a subagent) rebuilds the prefix and the
older session's hit is lost. opencode (`session/affinity.ts`) keys by session group instead:
`session.parentID ?? session.fork?.sessionID ?? session.id` — children and forks inherit the parent's
key, so a subagent that sends the same prefix lands in the same slot.

The cwd key stays as the fallback for callers that have no session id: it is still byte-stable.
"""
import hashlib

from . import config, servers
from .util import hash_cwd

# session id → group key. A child/fork inherits the group of its parent, so the whole family shares
# one prefix slot instead of resetting each other.
GROUPS = {}


def group(session_id=None, parent=None):
    """Affinity group of a session: parent first, then the session itself."""
    key = str(parent or "") or str(session_id or "")
    if not key:
        return None
    if key in GROUPS:
        return GROUPS[key]
    root = str(parent or "") or key
    GROUPS[key] = root
    return root


def cache_key(cwd=None, session_id=None, parent=None):
    """Return a stable key with a collision-resistant session affinity suffix.

    Session IDs in this project are timestamps (YYYYMMDD-HHMMSS). Taking the first 12
    characters made every session in the same ten-minute bucket share one cache slot,
    causing active sessions/subagents to evict one another's conversation prefix.
    """
    group_key = group(session_id, parent)
    suffix = "" if not group_key else "-" + hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:16]
    return "maxedcode-" + hash_cwd(cwd) + suffix


def fields():
    """Cache fields the current server takes (servers.cache_fields); unknown kind = the full Strata set."""
    return servers.cache_fields(config.SERVER_KIND)


def prompt_cache():
    """{"cache_prompt": True} for llama.cpp-family servers, nothing for the rest."""
    return {"cache_prompt": True} if "cache_prompt" in fields() else {}


def pin(body, messages):
    """Pins the Strata checkpoint after `messages` messages; other servers do not get the field."""
    if "strata_prefix" in fields():
        body["strata_prefix"] = {"messages": int(messages)}
    else:
        body.pop("strata_prefix", None)
    return body


def extra_body(cwd=None, session_id=None, parent=None):
    # Unsupported servers may reject unknown extra-body fields; callers already have a fallback.
    # strata_prefix pins the checkpoint at the end of the system turn (the first message). The key
    # alone does not pin it: a new session gets its own slot, so without the pin the whole prefix is
    # read again. The engine checkpoints a prompt read from token 0 at its first turn boundary
    # (generate.cpp: prompt_cache_root) only when the system turn is identical for every session -
    # which is why nothing per-session (cwd, date) may be inside SYSTEM.
    # Only the plain {"messages": K} form is sent: a malformed field is a 400, and a request with
    # images or a single message gets the pin ignored by the server, never refused.
    # Other servers get only what they understand (servers.cache_fields): llama.cpp cache_prompt, OpenAI its
    # prompt_cache_key (same affinity key), LM Studio / Ollama / vLLM nothing - they reuse the prefix themselves.
    want, key = fields(), cache_key(cwd, session_id, parent)
    body = {**prompt_cache()}
    if "promptCacheKey" in want:
        body["promptCacheKey"] = key
    if "prompt_cache_key" in want:
        body["prompt_cache_key"] = key
    return pin(body, 1)


def subagent_body(cwd=None, parent_session=None):
    """A subagent sends the same prefix as its parent, so it must use the parent's group key."""
    return extra_body(cwd, parent_session, parent_session)


def parent_of(session_id):
    """Group key that a subagent request must reuse (opencode affinity: parent -> children)."""
    return group(session_id, session_id)
