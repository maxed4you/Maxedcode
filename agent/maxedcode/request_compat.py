"""Narrow compatibility fallback for optional API request parameters.

A generic HTTP 400 is not evidence that sampling or cache parameters are unsupported. Only
remove named optional fields when the server error explicitly describes an unsupported or
unexpected field; never drop prompt-cache affinity because a tool-history request is malformed.
"""
import re

_REJECTION = re.compile(
    r"unrecognized|unknown (?:request )?(?:argument|parameter|field)|unsupported|unexpected|"
    r"extra[_ ]forbidden|extra field|additional propert|not permitted|not allowed|"
    r"does not support|is not supported|invalid parameter",
    re.IGNORECASE,
)
_KNOWN_FIELDS = {
    "cache_prompt", "promptCacheKey", "strata_prefix",
    "top_k", "min_p", "reasoning_effort", "reasoning_budget_tokens",
    "chat_template_kwargs", "stream_options",
}
_GENERATION_FIELDS = {
    "top_k", "min_p", "reasoning_effort", "reasoning_budget_tokens", "chat_template_kwargs",
}


def unsupported_optional_fields(message, candidates):
    """Return only explicitly rejected fields, or None if the error isn't a parameter rejection.

    Some OpenAI-compatible servers emit a generic "unsupported parameter" error without naming
    the field. In that case remove generation/sampling extras first, preserving cache controls.
    """
    message = str(message or "")
    if not _REJECTION.search(message):
        return None
    candidate_names = {str(name).lower(): str(name) for name in candidates}
    named = set()
    lowered = message.lower()
    for field in _KNOWN_FIELDS:
        canonical = candidate_names.get(field.lower())
        if canonical and re.search(r"(?<![a-z0-9_])" + re.escape(field.lower()) + r"(?![a-z0-9_])", lowered):
            named.add(canonical)
    if named:
        return named
    # Generic compatibility errors should not evict the cache affinity/prefix parameters.
    fallback = {candidate_names[field.lower()] for field in _GENERATION_FIELDS
                if field.lower() in candidate_names}
    return fallback or None
