"""Settings: settings.json (user → project → CLI → managed), then environment, then defaults.

Memory is no longer glued into the system prompt: it goes as a separate message after system
(memory.message), otherwise the Strata prefix cache is reset on every change of a memory file.
"""
from contextlib import suppress
from pathlib import Path
import json
import os
import re
import sys
import time
import urllib.parse

from . import servers
from .shelltool import POSIX

APP_DIR = Path(__file__).resolve().parent.parent
CFG = {}
with suppress(Exception):
    CFG = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))

def load_json(f):
    with suppress(Exception):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    return {}

def cli_settings(flags_only=False):
    """CLI arguments: --model, --base-url, --api-key, --ctx, --home, --settings <file>."""
    a, out = sys.argv[1:], {}
    keys = {"--model": "model", "--base-url": "base_url", "--api-key": "api_key", "--ctx": "ctx", "--home": "home"}
    for i, x in enumerate(a):
        if x in keys and i + 1 < len(a):
            out[keys[x]] = a[i + 1]
        elif x == "--settings" and i + 1 < len(a) and not flags_only:
            out.update(load_json(a[i + 1]))
    return out

# precedence: managed > CLI > project > user
SETTINGS = {**load_json(Path.home() / ".maxedcode" / "settings.json"),
            **load_json(Path.cwd() / ".maxedcode" / "settings.json"),
            **cli_settings(),
            **load_json(os.environ.get("MAXEDCODE_MANAGED_SETTINGS") or Path("C:/ProgramData/maxedcode/settings.json"))}
for k, v in (SETTINGS.get("env") or {}).items():
    os.environ.setdefault(str(k), str(v))

_FLAGS = cli_settings(flags_only=True)


def _get(env, key, default):
    """--flag > env > settings.json > config.json > default (an AGENT_BASE_URL left in the environment silently
    beat --base-url)"""
    return _FLAGS.get(key) or os.environ.get(env) or SETTINGS.get(key) or CFG.get(key) or default

BASE_URL = _get("AGENT_BASE_URL", "base_url", "http://localhost:8080/v1")
# localhost → 127.0.0.1: Windows tries ::1 first, while llama.cpp/Ollama listen on IPv4 only,
# and every connection waits ~2 s for the fallback (agent start — 4 requests = 8 s, plus every model request).
BASE_URL = re.sub(r"^(\w+://)localhost(?=[:/]|$)", r"\g<1>127.0.0.1", str(BASE_URL).strip(), flags=re.I)
# "127.0.0.1:8080" (no scheme) failed as an unsupported protocol and was retried for a minute and a half;
# a full ".../chat/completions" endpoint posted to .../chat/completions/chat/completions
if not re.match(r"^\w+://", BASE_URL):
    BASE_URL = "http://" + BASE_URL
BASE_URL = re.sub(r"/chat/completions/?$", "", BASE_URL.rstrip("/"), flags=re.I)
API_KEY = _get("AGENT_API_KEY", "api_key", "local")
MODEL = _get("AGENT_MODEL", "model", "local")
# Which server this is (servers.LABELS): decides the cache fields of a request and how the window is read.
# Set by the installer / first start ("server" in config.json); empty = found out at the first request.
SERVER_KIND = str(_get("AGENT_SERVER", "server", "")).strip().lower()
HOME = Path(_get("AGENT_HOME", "home", str(Path.home() / ".maxedcode")))
SESS, MCP_FILE, HIST_FILE = HOME / "sessions", HOME / "mcp.json", HOME / "input_history.jsonl"
STATE_FILE, PERMS_FILE = HOME / "state.json", HOME / "permissions.json"
MEM_DIR = HOME / "memory"
# Runtime sampling override set by the CLI (/sampling, /temp, /topk). Restored from state.json, so it
# survives restarts, and it wins over model_settings and env.
RUNTIME = {}
with suppress(Exception):
    for k, v in (load_json(STATE_FILE).get("sampling") or {}).items():
        if k in ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "frequency_penalty"):
            RUNTIME[k] = v
# Tool context limits and optional tools. AGENT_DEFER_TOOLS=0 disables
# deferred loading of MCP schemas; AGENT_FINISH_TOOL=0 removes the explicit end-of-turn tool.
MCP_DEFER = max(0, int(os.environ.get("MCP_DEFER", "12")))
DEFER_TOOLS = str(os.environ.get("AGENT_DEFER_TOOLS", "1")).lower() not in ("0", "false", "no")
FINISH_TOOL_ENABLED = str(os.environ.get("AGENT_FINISH_TOOL", "0")).lower() not in ("0", "false", "no")
MAX_FILES = max(1, int(os.environ.get("AGENT_MAX_FILES", "10")))
# USD per 1M tokens: (input, output, cached input). Values can be updated here;
# AGENT_PRICE sets the price for the current model and overrides the table: in:out:cached.
MODEL_PRICES = {
    "gpt-4o": (2.50, 10.00, 1.25),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "claude-3-5-sonnet": (3.00, 15.00, 0.30),
    "claude-3-7-sonnet": (3.00, 15.00, 0.30),
    "claude-sonnet-4.6": (3.00, 15.00, 0.30),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-opus-4.7": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-haiku-4.5": (1.00, 5.00, 0.10),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "gemini-2.5-flash": (0.30, 2.50, 0.03),
}
VIRTUAL_PRICE = (0.15, 0.60, 0.075)  # nominal price for local/unknown models

def _price_tuple(raw):
    try:
        vals = tuple(float(x.strip()) for x in str(raw).split(":"))
        return vals if len(vals) == 3 and all(x >= 0 for x in vals) else None
    except (TypeError, ValueError):
        return None

AGENT_PRICE = _price_tuple(os.environ.get("AGENT_PRICE", ""))

def model_price(model):
    """Return (input, output, cached-input price per 1M tokens, is_virtual)."""
    if AGENT_PRICE is not None:
        return (*AGENT_PRICE, False)
    name = str(model or "").lower()
    tail = name.rsplit("/", 1)[-1]
    for prefix, rates in sorted(MODEL_PRICES.items(), key=lambda x: len(x[0]), reverse=True):
        if tail == prefix or tail.startswith(prefix + "-"):
            return (*rates, False)
    return (*VIRTUAL_PRICE, True)

def usage_cost(usage, model=None):
    """Estimate USD from an OpenAI-compatible usage object; cached input is billed separately."""
    if not isinstance(usage, dict):
        return 0.0
    prompt = max(0, int(usage.get("prompt_tokens") or 0))
    completion = max(0, int(usage.get("completion_tokens") or 0))
    cached = max(0, min(prompt, int(usage.get("cached_tokens") or 0)))
    pin, pout, pcached, _ = model_price(model or MODEL)
    return ((prompt - cached) * pin + cached * pcached + completion * pout) / 1_000_000

def cost_text(cost, model=None):
    """Display estimated cost and mark the synthetic rate used for local/unknown models."""
    _, _, _, virtual = model_price(model or MODEL)
    return f"~${max(0.0, float(cost or 0)):.4f}" + (" virtual" if virtual else "")
# Protected paths: denied for read and for write even without a rule (permissions.py).
# AppData\Local\Programs is open: it holds installed programs (their configs and scripts), not browser profiles and tokens.
PROTECTED = [str(x) for x in (SETTINGS.get("protected_paths") or [])] or [r"\.env(?:\.|$)", r"\.ssh", r"(?<![A-Z0-9_])AppData(?![\\/]+Local[\\/]+(?:Temp|Programs)(?![\w.-]))"]


def posix_protected(home=None):
    """Default protected paths on Linux/macOS. Their AppData (app data, browser profiles, tokens) is ~/.config,
    ~/.local/share and ~/Library (its Caches stay open, like AppData\\Local\\Temp). System folders are protected
    only as a whole path (an edit_file target), so `cat /etc/os-release` or `/usr/bin/env python3` still run."""
    home_re = (r"(?:~|\$HOME|\$\{HOME\}|" + re.escape(str(home or Path.home()).rstrip("/")) +
               r"|/home/[^/\s]+|/Users/[^/\s]+|/root)")
    return [r"\.env(?:\.|$)", r"\.ssh", r"\.gnupg", home_re + r"/\.config(?:/|$)", home_re + r"/\.local/share(?:/|$)",
            home_re + r"/Library/(?!Caches(?:/|$))", r"^/(?:etc|usr|bin|sbin|boot|lib|lib64|System|private/etc)(?:/\S*)?$"]


if POSIX and not SETTINGS.get("protected_paths"):
    PROTECTED = posix_protected()

# system_prompt.md lies next to the entry script (agent / app folder), hence one level above the package.
PROMPT_FILE = Path(__file__).resolve().parent.parent / "system_prompt.md"
_BASE = PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else \
    ("You are an agent in bash. Answer briefly. Do only what is asked." if POSIX else
     "You are an agent in Windows PowerShell. Answer briefly. Do only what is asked.")
if POSIX:
    from .prompt_posix import posix_prompt
    _BASE = posix_prompt(_BASE)
# The prefix (system + tools) is built once and never rebuilt: Strata caches it.
# The environment block is deliberately NOT inside SYSTEM. The engine checkpoints a prompt read from
# token 0 at its first turn boundary - the end of the system turn - and every chat of the same client
# shares that checkpoint (generate.cpp: prompt_cache_root). A cwd or a date inside the system turn makes
# that turn different for every folder and every day, so a new session reads the whole prefix again.
# Sent as the first message after memory, the system turn stays byte-identical across sessions.
SYSTEM = _BASE.rstrip()


def _os_line():
    """The <env> OS line: Windows as before; on Linux/macOS the system name and version and the bash tool."""
    if not POSIX:
        return "OS: Windows; shell: Windows PowerShell (tool: powershell)"
    import platform
    name = "macOS " + platform.mac_ver()[0] if sys.platform == "darwin" else platform.system()
    with suppress(Exception):
        if sys.platform.startswith("linux"):
            name = "Linux (" + platform.freedesktop_os_release().get("PRETTY_NAME", "").strip() + ")"
    return f"OS: {name.replace(' ()', '').strip()}; shell: bash (tool: bash)"


ENV_BLOCK = (f"<env>\n{_os_line()}\n"
             f"Working directory: {os.getcwd()}\nUser home: {Path.home()}\nToday: {time.strftime('%Y-%m-%d, %A')}\n"
             # Without this line the model searched the disk to find the server when asked to switch models.
             # Kept neutral: a paragraph about "this CLI" made small models talk about it in unrelated chats.
             f"Model server (OpenAI-compatible): {BASE_URL}; config: {HOME / 'config.json'}\n</env>")
# Sampling. Without explicit values the server can run almost greedily, which makes Q2 loop.
SAMPLING = {"temperature": float(os.environ.get("AGENT_TEMP", "0.6")), "top_p": float(os.environ.get("AGENT_TOP_P", "0.95")),
            "presence_penalty": float(os.environ.get("AGENT_PRESENCE", "0"))}
TOP_K = int(os.environ.get("AGENT_TOP_K", "20"))


def model_profile(model=None):
    """Settings for a model: exact id wins, otherwise the longest matching prefix."""
    profiles = SETTINGS.get("model_settings") or SETTINGS.get("models") or {}
    if not isinstance(profiles, dict):
        return {}
    name = str(model or MODEL).strip().lower()
    tail = name.rsplit("/", 1)[-1]
    normalized = {str(k).strip().lower(): v for k, v in profiles.items() if isinstance(v, dict)}
    if name in normalized:
        return normalized[name]
    if tail in normalized:
        return normalized[tail]
    matches = [(key, value) for key, value in normalized.items()
               if key and (name.startswith(key) or tail.startswith(key))]
    return max(matches, key=lambda item: len(item[0]))[1] if matches else {}


def sampling_for(model=None):
    """Base sampling with optional per-model overrides from settings.json."""
    profile = model_profile(model)
    result = dict(SAMPLING)
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        value = profile.get(key, profile.get("temp") if key == "temperature" else None)
        if value is not None:
            try:
                result[key] = float(value)
            except (TypeError, ValueError):
                pass
    for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if key in RUNTIME:
            result[key] = RUNTIME[key]
    return result


MIN_P = float(os.environ.get("AGENT_MIN_P", "0.05"))


def min_p_for(model=None):
    """min_p: profile "min_p" > AGENT_MIN_P > 0.05 (runtime /sampling wins over both)."""
    try:
        return float(RUNTIME.get("min_p", model_profile(model).get("min_p", MIN_P)))
    except (TypeError, ValueError):
        return MIN_P


def top_k_for(model=None):
    try:
        return int(RUNTIME.get("top_k", model_profile(model).get("top_k", TOP_K)))
    except (TypeError, ValueError):
        return TOP_K


def max_turns_for(model=None, default=None):
    fallback = default if default is not None else SETTINGS.get("max_turns", 50)
    try:
        return max(1, int(model_profile(model).get("max_turns", fallback)))
    except (TypeError, ValueError):
        return max(1, int(fallback))


def edit_format_for(model=None):
    value = str(model_profile(model).get("edit_format", "exact")).lower().replace("-file", "")
    return value if value in {"exact", "whole", "udiff"} else "exact"
CTX_WINDOW = int(_get("AGENT_CTX", "ctx", 32768))   # model context window size on the server
# The old default sent almost the entire context window (often ~30k tokens) as a reasoning
# budget. That is excessive for a local agent and can make tiny requests slow. A positive
# AGENT_THINK_BUDGET is an explicit override; 0 uses bounded adaptive defaults.
ANSWER_ROOM = int(os.environ.get("AGENT_ANSWER_ROOM", "1024"))
ANSWER_ROOM_HIGH = int(os.environ.get("AGENT_ANSWER_ROOM_HIGH", "2048"))
MIN_THINK = max(1, int(os.environ.get("AGENT_MIN_THINK", "256")))
THINK_BUDGET = max(0, int(os.environ.get("AGENT_THINK_BUDGET", "0")))
THINK_CAP = max(MIN_THINK, int(os.environ.get("AGENT_THINK_CAP", "4096")))
THINK_CAP_LOW = max(MIN_THINK, int(os.environ.get("AGENT_THINK_CAP_LOW", "1024")))
THINK_CAP_HIGH = max(THINK_CAP, int(os.environ.get("AGENT_THINK_CAP_HIGH", "16384")))
# A step that follows successful tool results continues a plan made earlier in the turn: it needs a
# short look at the new output, not a fresh deep think (that is where a local model "sits and thinks").
# The first step of a turn and a step after an error keep the full cap. 0 disables the step cap.
THINK_CAP_STEP = max(0, int(os.environ.get("AGENT_THINK_CAP_STEP", "2048")))
# The step after a looped reasoning was cut: a short think. reasoning_effort=none would change the system
# turn the template renders the effort into, and the server would re-read the whole conversation.
LOOP_RETRY_THINK = max(MIN_THINK, int(os.environ.get("AGENT_LOOP_RETRY_THINK", "256")))
_STEP_ERROR = re.compile(r"^\s*(Error|Tool error|Not executed|Interrupted|The user rejected)|exit code [1-9]|Traceback \(most recent", re.I)

# Language of the model's reasoning (the answer stays in the user's language). "" leaves it to the model.
THINK_LANG = str(_get("AGENT_THINK_LANG", "think_language", "English")).strip()
THINK_LANG = "" if THINK_LANG.lower() in ("none", "off", "0", "false", "auto") else THINK_LANG
# "think hard" / "ultrathink" / "подумай хорошо" in a message: the full high cap for that turn only.
DEEP_THINK_RE = re.compile(r"\bultrathink\b|\bthink\s+(?:hard|harder|deeply|carefully)\b|"
                           r"подумай\s+(?:хорошо|хорошенько|как следует|глубоко|тщательно|получше)|"
                           r"(?:хорошенько|глубоко|тщательно)\s+подумай", re.I)

_READ_TOOLS = {"read_file", "grep", "glob", "py_source", "web_search", "fetch_url", "tool_search", "skill"}


def _first_reads_of_turn(msgs):
    """Every assistant step of the turn so far only read (the task, the code, the tests): no plan exists yet,
    the next step is where the model works out the solution, so it must not get the short step cap.
    Any number of read steps: live R4 read the task, then the tests in a second step, and the solution
    was written under the 1024-token step cap, then fixed by trial over 47 more steps."""
    names = []
    for m in reversed(msgs or []):
        if not isinstance(m, dict) or m.get("role") == "user":
            break
        if m.get("role") == "assistant":
            calls = m.get("tool_calls") or []
            if not calls:
                return False
            names += [(c.get("function") or {}).get("name") for c in calls]
    return bool(names) and all(n in _READ_TOOLS for n in names)


def quick_step(msgs):
    """True when the request continues a turn right after tool results that all succeeded."""
    tail = []
    for m in reversed(msgs or []):
        if not isinstance(m, dict) or m.get("role") != "tool":
            break
        tail.append(m)
    if not tail or _first_reads_of_turn(msgs):
        return False
    for m in tail:
        body = m.get("content")
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        if _STEP_ERROR.search(text[:4000]):
            return False
    return True

def norm_effort(effort):
    """User-facing level (off/low/medium/high/ultra). Legacy values: "none" is off, "default" and unset are medium."""
    e = str(effort or "").strip().lower()
    return {"none": "off", "default": "medium", "": "medium"}.get(e, e)


def wire_effort(effort):
    """The level the server and the budgets see: off is "none", ultra reasons at "high" (its staging is ultra.py)."""
    e = str(effort or "").lower()
    return {"off": "none", "ultra": "high"}.get(e, e)


def answer_room(effort=""):
    """Tokens reserved for the answer: a long final answer needs more room than a short one."""
    effort = wire_effort(effort)
    return ANSWER_ROOM_HIGH if effort in ("medium", "high", "default") else ANSWER_ROOM

_WEB_TOOLS = {"web_search", "fetch_url"}


def planning_step(msgs):
    """True for the step right after the turn's first reads: the one that works out the whole solution.

    Not after web results: a research turn never writes, so every step after a search counted as the
    planning step and got the high cap (live X5: five steps of ~8200 reasoning tokens, 60-150 s each,
    spent listing sports a score might belong to). One long think pays off on the code and tests just
    read, not on a page of search results; those steps keep the normal cap."""
    tail = msgs[-1] if msgs else None
    if not (isinstance(tail, dict) and tail.get("role") == "tool" and _first_reads_of_turn(msgs)):
        return False
    last = next((m for m in reversed(msgs) if isinstance(m, dict) and m.get("role") == "assistant"), None)
    names = {(c.get("function") or {}).get("name") for c in (last or {}).get("tool_calls") or []}
    return not names & _WEB_TOOLS


def think_budget(ptok=0, effort="", step=False, plan=False):
    """Return a bounded reasoning budget, or 0 to let the server choose.

    A positive AGENT_THINK_BUDGET overrides the adaptive cap. Otherwise effort chooses a
    conservative cap; the available context remains the hard upper bound.
    step=True (see quick_step) lowers the low/medium cap to THINK_CAP_STEP.
    plan=True (see planning_step) raises the medium cap to THINK_CAP_HIGH for that one step: one long
    think there, as in a single direct request, is cheaper than solving by trial and error over many steps.
    """
    effort = wire_effort(effort)
    try:
        free = max(0, CTX_WINDOW - max(0, int(ptok or 0)) - answer_room(effort))
    except (TypeError, ValueError):
        free = max(0, CTX_WINDOW - answer_room(effort))
    if free < MIN_THINK or effort == "none":
        return 0
    profile = model_profile()
    profile_budget = profile.get("reasoning_budget_tokens") if isinstance(profile, dict) else None
    if THINK_BUDGET > 0:
        cap = THINK_BUDGET
    elif profile_budget is not None:
        try:
            cap = max(0, int(profile_budget))
        except (TypeError, ValueError):
            cap = THINK_CAP
    elif effort == "low":
        cap = THINK_CAP_LOW
    elif effort == "high":
        cap = THINK_CAP_HIGH
    else:
        cap = THINK_CAP
    if step and THINK_CAP_STEP and THINK_BUDGET <= 0 and profile_budget is None and effort in ("low", "medium"):
        cap = min(cap, max(MIN_THINK, THINK_CAP_STEP))
    elif plan and THINK_BUDGET <= 0 and profile_budget is None and effort == "medium":
        cap = max(cap, THINK_CAP_HIGH)
    return min(free, cap) if cap >= MIN_THINK else 0
def effort_body(effort):
    """Request fields for reasoning depth. Without reasoning_effort the Qwen template uses xhigh:
    on a simple task that means 10k+ reasoning tokens and an empty answer. "default" — the server decides."""
    e = wire_effort(effort)
    if not e or e == "default":
        return {}
    if e == "none":
        return {"reasoning_effort": e, "chat_template_kwargs": {"enable_thinking": False}}
    return {"reasoning_effort": e}


def summary_effort(effort):
    """The compaction summary needs no deep reasoning: it rewrites the history, it doesn't solve the task."""
    e = wire_effort(effort)
    return e if e in ("none", "low") else "low"
# How servers word the thinking-budget error (the reply was cut off while still thinking).
BUDGET_RE = re.compile(r"reasoning_budget|thinking budget|reached max tokens while still thinking", re.I)
THINK_CUT_NUDGE = ("<thinking_cut>Your previous reply ran out of tokens while still thinking, so it had no answer. "
                   "Do not re-think from scratch: finish the thought in a few lines and then answer.</thinking_cut>")
LENGTH_CONTINUES = 2      # a text answer cut at the output limit is continued at most this many times per turn


def length_continue(visible):
    """The message after a text reply the server cut at the output limit (finish_reason "length")."""
    tail = " ".join(str(visible).split())[-160:]
    return ("<reply_cut>Your reply was cut off by the output limit. Continue it exactly where it stopped, "
            "without repeating what is already written and without a preamble. It ended with: "
            f"\"…{tail}\"</reply_cut>")


CTX_SRC = "AGENT_CTX env" if os.environ.get("AGENT_CTX") else ("settings.json" if SETTINGS.get("ctx") else "default")
# Some chat templates reject a second system message (memory) or a system message in the middle of the
# history (rules, hooks). AGENT_SYSTEM_AS_USER=1 (or "system_as_user": true in settings.json) sends every
# system message except the first one as a user message. Append-only, so the prefix cache is not affected.
SYSTEM_AS_USER = str(_get("AGENT_SYSTEM_AS_USER", "system_as_user", "0")).lower() in ("1", "true", "yes")
# Prefix-cache safety. Chat templates (Qwen & co.) render the reasoning of an assistant message only
# while it belongs to the current turn, i.e. after the last user message. When the next user message
# arrives, every assistant message of the previous turn is re-rendered WITHOUT its <think> block, the
# prompt no longer matches the server's KV cache from that point on, and the whole previous turn is
# re-read. So reasoning_content is not sent back by default (AGENT_KEEP_REASONING=1 restores it).
# Templates with preserve_thinking (the server's chat_template_caps.supports_preserve_reasoning) render
# every assistant message with its <think> block, wherever it is: there sending the reasoning back is
# what keeps the history equal to what the model generated, so the next step reads less again, and the
# model keeps the notes it made while reading. "auto" (default) asks the server once: resolve_keep_reasoning().
_KEEP_RAW = str(_get("AGENT_KEEP_REASONING", "keep_reasoning", "auto")).lower()
KEEP_REASONING = _KEEP_RAW in ("1", "true", "yes")

def resolve_keep_reasoning(props=None):
    """auto: send reasoning back only when the server's chat template preserves it. Safe default: off.
    props: an already read /props answer (resolve_server passes it, so the server is not asked twice)."""
    global KEEP_REASONING
    if _KEEP_RAW != "auto":
        return KEEP_REASONING
    if props is None and SERVER_KIND in ("strata", "llamacpp", ""):
        props = servers.get(servers.root(BASE_URL) + "/props", API_KEY)
    if isinstance(props, dict):
        KEEP_REASONING = bool((props.get("chat_template_caps") or {}).get("supports_preserve_reasoning"))
    return KEEP_REASONING


MODEL_SET = bool(os.environ.get("AGENT_MODEL") or SETTINGS.get("model") or CFG.get("model"))


def resolve_server():
    """Ask the server once for what the user did not set: its kind, the real window, the model id, reasoning support.

    Without the id the model is "local": no model_settings profile matches, and LM Studio / Ollama (which list
    every downloaded model) refuse it or load another one. Without the window the agent compacts too late and
    the server cuts the prompt itself (Ollama silently, from the front), so the cache breaks on every request."""
    global SERVER_KIND
    info = servers.describe(BASE_URL, API_KEY, SERVER_KIND, _want())
    SERVER_KIND = info["kind"]
    _adopt(info.get("use") or (None if MODEL_SET else info["model"]), info["ctx"], info.get("loaded_ctx"))
    if info["online"]:                  # an off server is not asked /props once more (2 s refused connect)
        resolve_keep_reasoning(info["props"])
    return {"ctx": CTX_WINDOW, "model": MODEL, "keep_reasoning": KEEP_REASONING, "kind": SERVER_KIND,
            "online": info["online"], "note": info.get("note") or ""}


CHOSEN = ""     # the configured model while the one loaded on the server is used instead (servers.pick_model)


def _want():
    """The model the user chose (--model, env, settings.json, config.json), or None: the server's own."""
    return (CHOSEN or MODEL) if MODEL_SET else None


def _adopt(model, ctx, loaded_ctx):
    """Use what the server reported: the model requests name, its window (unless the user set one; a set
    window larger than the loaded model has would overflow it, so the loaded one wins then)."""
    global MODEL, CHOSEN, CTX_WINDOW, CTX_SRC
    if model and str(model) != MODEL:
        if MODEL_SET and not CHOSEN:
            CHOSEN = MODEL
        MODEL = str(model)
    label = servers.LABELS.get(SERVER_KIND) or "reported"
    if ctx and (CTX_SRC == "default" or CTX_SRC.startswith("server")):
        CTX_WINDOW, CTX_SRC = int(ctx), f"server ({label})"
    elif loaded_ctx and int(loaded_ctx) < CTX_WINDOW:
        CTX_WINDOW, CTX_SRC = int(loaded_ctx), f"server ({label}, as loaded)"


def follow_loaded(timeout=1.5):
    """LM Studio / Ollama: the model loaded there now, in one request (the user may switch it between turns).

    The TUI asks before each turn, headless with AGENT_CTX set (resolve_server is skipped then). Other servers
    and a server that is off cost nothing / 0.5 s (port_down). -> {"model", "ctx", "note"}"""
    global SERVER_KIND
    kind = SERVER_KIND or {1234: "lmstudio", 11434: "ollama"}.get(urllib.parse.urlsplit(BASE_URL).port, "")
    if kind not in servers.JIT_KINDS or servers.port_down(BASE_URL):
        return {"model": MODEL, "ctx": CTX_WINDOW, "note": ""}
    on = servers.loaded_models(BASE_URL, API_KEY, kind, timeout)
    model, loaded_ctx, note = servers.pick_model(_want(), on, kind)
    if on is not None:
        SERVER_KIND = kind
        _adopt(model, loaded_ctx, loaded_ctx)
    return {"model": MODEL, "ctx": CTX_WINDOW, "note": note}


def use_server(base, kind="", model=None):
    """Switch this process to another model server (CLI /server, the window's picker); the caller saves it.

    The window, the model and the server kind are found again; a context from the environment or settings.json
    stays. -> resolve_server()'s answer."""
    global BASE_URL, SERVER_KIND, MODEL, MODEL_SET, CTX_SRC, CHOSEN
    base = str(base).strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        base = "http://" + base
    if not re.search(r"/v\d+$", base) and not re.match(r"^https?://[^/]+/.", base, re.I):
        base += "/v1"
    BASE_URL = re.sub(r"^(\w+://)localhost(?=[:/]|$)", r"\g<1>127.0.0.1", base, flags=re.I)
    SERVER_KIND = str(kind or "").lower()
    MODEL, MODEL_SET = (str(model), True) if model else ("local", False)
    CHOSEN = ""
    if CTX_SRC not in ("AGENT_CTX env", "settings.json"):
        CTX_SRC = "default"
    return resolve_server()


# tool_choice="none" is not guaranteed to leave the rendered prompt untouched (some servers drop the tools
# from the template for it). Off by default: tool_choice stays "auto" in every request of a session.
TOOL_CHOICE_NONE = str(_get("AGENT_TOOL_CHOICE_NONE", "tool_choice_none", "0")).lower() in ("1", "true", "yes")
# A short final reply after tool work is checked by the model itself: "did it only announce a step?" (any
# language; see loopguard.judge_stalled). One small extra request on such turns; "promise_check": false turns it off.
PROMISE_CHECK = str(_get("AGENT_PROMISE_CHECK", "promise_check", "1")).lower() not in ("0", "false", "no", "off")
# AGENT_CACHE_DEBUG=1: before every request print where it differs from the previous one (see cachedebug.py).
CACHE_DEBUG = str(_get("AGENT_CACHE_DEBUG", "cache_debug", "0")).lower() in ("1", "true", "yes")
# Headless sends one plain (non-streamed) request per step. Streamed, Strata did not recognise its own reply in
# the next request after long replies and re-read all of it (live LCB 2026-11-01: 0 such re-reads without
# streaming, 2-3 per task with it; one cost 57 s). "headless_stream": true brings back the stream, which can
# cut a looping reasoning early.
HEADLESS_STREAM = str(_get("AGENT_HEADLESS_STREAM", "headless_stream", "0")).lower() in ("1", "true", "yes")
# The same switch for the TUI and for subagents. Still streamed by default (live text, early cut of a looping
# reasoning) until their re-reads are measured on the server; "tui_stream": false / "subagent_stream": false
# send plain requests (the TUI then shows the reply when it is complete).
TUI_STREAM = str(_get("AGENT_TUI_STREAM", "tui_stream", "1")).lower() not in ("0", "false", "no", "off")
SUBAGENT_STREAM = str(_get("AGENT_SUBAGENT_STREAM", "subagent_stream", "1")).lower() not in ("0", "false", "no", "off")
MAX_OUT = max(1500, min(6000, CTX_WINDOW // 4))     # clip tool results to fit a small window
# auto-compact at this fill percentage. The in-place summary needs ~3000 free tokens (compact.INPLACE_ROOM):
# at 96% of a 32k window only ~1300 are left, so it never fit and the lossy transcript was always used.
COMPACT_AT = 90
COMPACT_KEEP = 6                                        # explicit /compact N = preserve N recent messages
try:
    COMPACT_KEEP_TOKENS = max(1000, int(_get("AGENT_COMPACT_KEEP_TOKENS", "compaction_keep_tokens", 6000)))
except (TypeError, ValueError):
    COMPACT_KEEP_TOKENS = 6000
TOOL_OUTPUT_LIMIT = max(4000, int(os.environ.get("AGENT_TOOL_OUTPUT_LIMIT", "12000")))


def read_output_limit():
    """read_file may return more than other tools: a source file the model needs whole (an 11 KB test
    file) was spilled at 12k chars, read again in a second request from a copy with doubled line numbers.
    ~30% of the window in chars (about 9% in tokens), at most 24k; never below TOOL_OUTPUT_LIMIT."""
    return max(TOOL_OUTPUT_LIMIT, min(24_000, CTX_WINDOW * 3 // 10))


def output_limit(tool_name):
    """Character limit of one tool result before it is spilled to a file (util.normalize_tool_output)."""
    return read_output_limit() if tool_name == "read_file" else TOOL_OUTPUT_LIMIT
CODE_MODE_TIMEOUT = max(1, min(120, int(os.environ.get("AGENT_CODE_MODE_TIMEOUT", "45"))))
# Reserved ceiling for future server-capacity experiments. Current local subagent inference is
# hard-serialized at 1 in subagent._inference_semaphore; this env setting never starts parallel agents.
try:
    SUBAGENT_MAX_CONCURRENT = max(1, min(3, int(os.environ.get("AGENT_SUBAGENT_PARALLEL", "1"))))
except (TypeError, ValueError):
    SUBAGENT_MAX_CONCURRENT = 1
COMPACT_CATALOG = str(_get("AGENT_COMPACT_CATALOG", "compact_catalog", "1")).lower() not in ("0", "false", "no")
# code_mode is off by default: parallel calls in one reply and py_run cover it, a small model mistakes it for a
# Python runner, and its inner catalog is the largest tool schema. "code_mode": true in settings.json brings it back.
CODE_MODE = str(_get("AGENT_CODE_MODE", "code_mode", "0")).lower() in ("1", "true", "yes")
# The user's own browser (tools/browser.py, maxedcode/chrome): off until the user turns it on
# (`python -m maxedcode.chrome on` or the window's Settings), so the catalog only changes when they do.
BROWSER_TOOL = str(_get("AGENT_BROWSER", "browser_tool", "0")).lower() in ("1", "true", "yes", "on")
COMPACT_MAX_CHARS = 20_000                              # max history characters the summarizer sees
# Summarizer reserve, taken from opencode compaction.ts: while the transcript fits inside
# (window - reserve) it is sent unchanged, so the request prefix keeps its cache hit.
RESERVE_SHARE = float(os.environ.get("AGENT_RESERVE_SHARE", "0.15"))
RESERVE_MIN = max(1000, int(os.environ.get("AGENT_RESERVE_MIN", "16000")))
SHRINK_STEPS = (0.7, 0.5, 0.35)
TOOL_OUTPUT_MAX_CHARS = max(200, int(os.environ.get("AGENT_SUMMARY_TOOL_CHARS", "1250")))
# Stable image cut (opencode model-request.ts boundImages): images are dropped oldest-first only
# when the media size passes TRIGGER, and only down to TARGET, so the cut does not move on every
# request — a moved cut means a changed prefix and a lost cache hit.
IMAGE_BYTES_TRIGGER = max(100_000, int(os.environ.get("AGENT_IMAGE_BYTES_TRIGGER", "2000000")))
IMAGE_BYTES_TARGET = max(50_000, int(os.environ.get("AGENT_IMAGE_BYTES_TARGET", "1500000")))
# Models that cannot read media: images are replaced by a text stub instead of being sent.
# Empty (default) means "assume vision" — the model name alone does not prove capabilities.
NO_VISION = str(_get("AGENT_NO_VISION", "no_vision", "")).lower() in ("1", "true", "yes")
# Long local generations exceed a fixed fetch timeout; the timeout is lifted while a signal still
# allows cancellation (opencode no-timeout-fetch.ts).
# The page text has its own budget, separate from MAX_OUT: a fetched page is one tool result, and
# head/tail fit only makes sense for it when it is clearly bigger than the normal tool output.
FETCH_MAX_CHARS = max(2000, int(os.environ.get("AGENT_FETCH_MAX_CHARS", "12000")))
FETCH_TIMEOUT = int(os.environ.get("AGENT_FETCH_TIMEOUT", "30"))
# Default off (was on): with no timeout a server that stopped answering held the turn forever.
NO_FETCH_TIMEOUT = str(_get("AGENT_NO_FETCH_TIMEOUT", "no_fetch_timeout", "0")).lower() in ("1", "true", "yes")
# How servers word the context overflow: the number in brackets is the real n_ctx on the server.
CTX_SIZE_RE = re.compile(r"(?:the context|context size|context length|n_ctx)\s*\((\d+)(?: tokens)?\)|(?:context size|context length|maximum context length)\s*(?:is|of)\s*(\d+)", re.I)
