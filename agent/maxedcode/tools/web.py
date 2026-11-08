"""web_search and fetch_url.

The fetch timeout (AGENT_FETCH_TIMEOUT, 30 s) is a socket timeout: it limits silence, not the whole download,
so a long but live response still arrives. `no_fetch_timeout` turns it off. In Python a blocking urlopen
can't be interrupted from outside, so the open response is registered and closed on cancel — the same
effect as abort on a signal.
"""
import asyncio
import gzip
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import suppress

from ddgs import DDGS

from ..config import FETCH_MAX_CHARS, FETCH_TIMEOUT, NO_FETCH_TIMEOUT
from ..util import clip, save_full

OPEN = {}          # url -> response, so an outside cancel can close it
SEARCH_BACKENDS = ("auto", "yahoo", "bing")
FETCH_MAX_BYTES = 10_000_000   # a page body at most; an endless or huge response no longer fills memory
TEXT_TYPES = ("json", "xml", "html", "javascript", "ecmascript", "yaml", "toml", "csv", "markdown", "x-sh",
              "x-python", "x-www-form-urlencoded")


def _decode(data, headers):
    """Body bytes -> text. Some servers (python.org) gzip the answer even without Accept-Encoding."""
    enc = (headers.get("Content-Encoding") or "").lower()
    with suppress(Exception):
        if "gzip" in enc or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        elif "deflate" in enc:
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)   # raw deflate without the zlib header
    charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
    try:
        return data.decode(charset or "utf-8", "replace")
    except LookupError:
        return data.decode("utf-8", "replace")


# Menu/header/footer come first and ate the whole limit: the model saw only navigation and fetched again.
_DROP = re.compile(r"(?is)<(script|style|nav|header|footer|svg|noscript|aside|form|button|select|template|iframe|dialog)\b.*?</\1\s*>")
_BLOCK = re.compile(r"(?i)<(?:br|hr)\b[^>]*>|</?(?:p|div|li|ul|ol|tr|table|h[1-6]|section|article|main|blockquote|pre|dd|dt|figcaption)\b[^>]*>")


def _region(raw):
    """The page's own <main>, or its only <article>: menus, sidebars and footers stay out."""
    m = re.findall(r"(?is)<main\b[^>]*>(.*)</main>", raw)
    if len(m) == 1:
        return m[0]
    a = re.findall(r"(?is)<article\b[^>]*>(.*?)</article>", raw)
    return a[0] if len(a) == 1 else None


_TAG = re.compile(r"""<[a-zA-Z/!](?:"[^"]*"|'[^']*'|[^'">])*>""")
_PRE = re.compile(r"(?is)<pre\b[^>]*>(.*?)</pre\s*>")


def _text(raw):
    """Block tags become line breaks and table cells " | ": one line per row instead of one line per page.

    <pre> blocks keep their line breaks and indentation: collapsing whitespace turned
    "def f(x):\\n    if x:" into "def f(x):\\nif x:", and the model copied documentation code without indents."""
    blocks = []

    def keep(m):
        blocks.append(html.unescape(_TAG.sub("", m.group(1))).strip("\n"))
        return f"\n\x00{len(blocks) - 1}\x00\n"
    raw = _PRE.sub(keep, raw)
    raw = re.sub(r"(?i)</t[dh]\s*>", " | ", _BLOCK.sub("\n", raw))
    # Quoted attribute values may hold ">" (Wikipedia's data-mw JSON): a bare <[^>]+> left them as text.
    t = re.sub(r"[^\S\n]+", " ", html.unescape(_TAG.sub(" ", raw)))
    t = re.sub(r"(?:\| ?){2,}", "| ", t)                 # empty cells
    t = re.sub(r"\s*\n\s*", "\n", t)
    t = re.sub(r"\n\|(?= ?\n| ?$)", "", t).strip()      # a line that is only a cell border
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], t) if blocks else t


def _clean(raw):
    title = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", raw)
    title = re.sub(r"\s+", " ", html.unescape(title.group(1))).strip() if title else ""
    body = _DROP.sub(" ", re.sub(r"(?is)<head\b.*?</head>", " ", raw, count=1))
    full = _text(body)
    region = _region(body)
    if region:
        part = _text(region)
        # A JS shell's <main> holds almost nothing: then the whole page is the better guess.
        if len(part) >= min(1500, len(full) // 3):
            full = part
    return f"{title}\n{full}" if title and not full.startswith(title[:40]) else full


# A model page on Hugging Face is a JS app: the text holds only navigation. The API returns the same
# information (files with sizes, context, license) and the model card compactly.
HF_REPO = re.compile(r"^https?://(?:www\.)?huggingface\.co/(?!(?:api|models|datasets|spaces|docs|blog|papers|"
                     r"collections|organizations|settings|login|join|pricing|search|tasks)/)"
                     r"([\w.-]+/[\w.-]+)(?:/tree/[^?#]*)?/?(?:[?#].*)?$")


HF_SEARCH = re.compile(r"^https?://(?:www\.)?huggingface\.co/(?:api/)?models\?(?:.*&)?search=([^&#]+)")


def _gb(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def _hf_model(repo, get):
    raw = get(f"https://huggingface.co/api/models/{repo}?blobs=true")
    if raw.startswith("Error"):
        return None
    try:
        meta = json.loads(raw)
    except ValueError:
        return None
    out = [f"[Hugging Face model {meta.get('id', repo)} — read via the HF API; page text is not needed]",
           f"pipeline: {meta.get('pipeline_tag')}; downloads (last 30 days): {meta.get('downloads')}; likes: {meta.get('likes')}; "
           f"updated: {str(meta.get('lastModified'))[:10]}; gated: {meta.get('gated')}",
           "tags: " + ", ".join(map(str, (meta.get("tags") or [])[:20]))]
    gguf = meta.get("gguf") or {}
    if gguf:
        out.append(f"gguf: architecture {gguf.get('architecture')}, context {gguf.get('context_length')}, "
                   f"parameters {gguf.get('total')}")
    files = [s for s in meta.get("siblings") or [] if s.get("rfilename")]
    if files:
        out.append(f"files ({len(files)}):")
        out += [f"  {s['rfilename']}  {_gb(s['size']) if s.get('size') else ''}".rstrip() for s in files[:60]]
    card = get(f"https://huggingface.co/{repo}/raw/main/README.md")
    if not card.startswith("Error"):
        card = re.sub(r"(?s)^---\n.*?\n---\n", "", card).strip()
        out += ["", "--- model card (README.md) ---", card[:6000] + ("\n…[model card cut]" if len(card) > 6000 else "")]
    return "\n".join(out)


async def search(query):
    # The search engine sometimes answers with an error/rate limit: other engines, then a clear error
    # instead of a traceback — otherwise a weak model gives up searching and goes looking on the disk.
    # "auto" often answers "No results found" for a query another engine answers at once (measured:
    # 2 of 5 queries; yahoo answered all 5), so the retry goes to other engines, not the same one.
    err = None
    for backend in SEARCH_BACKENDS:
        try:
            res = await asyncio.to_thread(lambda: DDGS().text(query, max_results=6, backend=backend))
            if res:
                break
        except Exception as e:
            err = e
        res = None
    if res is None and err is None:
        res = []                 # every engine answered, none found anything
    if res is None:
        return (f"Error: web search failed ({type(err).__name__}: {str(err)[:200]}). Retry once with other words, "
                "or fetch_url a known site directly (e.g. https://huggingface.co/api/models?search=NAME for AI models).")
    return clip("\n\n".join((f"{x['title']}\n{x['href']}\n{x['body']}" for x in res))) or \
        f"Nothing found for: {query}. Try 2-4 different words (English names), or fetch_url the official site."


def _not_text(headers):
    """The Content-Type when the body is not text (PDF, archive, image, binary), else ""."""
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ctype or ctype.startswith("text/") or any(x in ctype for x in TEXT_TYPES):
        return ""
    return ctype


async def fetch(url):
    def get(u):
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        # A socket timeout limits silence, not the whole download: a slow but live page still arrives,
        # a server that stopped answering no longer holds the turn forever (headless cannot cancel it).
        timeout = None if NO_FETCH_TIMEOUT else FETCH_TIMEOUT
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            return f"Error: fetch failed: {type(e).__name__}: {e}"
        OPEN[url] = resp
        try:
            ctype = _not_text(resp.headers)
            if ctype:
                size = resp.headers.get("Content-Length") or "unknown"
                return (f"Error: fetch_url got {ctype} ({size} bytes), not a text page. Download it with powershell "
                        f"(Invoke-WebRequest -Uri <url> -OutFile <file>) and inspect the file with py_run.")
            data = resp.read(FETCH_MAX_BYTES + 1)
            cut = len(data) > FETCH_MAX_BYTES
            text = _decode(data[:FETCH_MAX_BYTES], resp.headers)
            return text + (f"\n…[download stopped at {FETCH_MAX_BYTES // 1_000_000} MB]" if cut else "")
        except Exception as e:
            return f"Error: fetch stopped: {type(e).__name__}: {e}"
        finally:
            OPEN.pop(url, None)
            with suppress(Exception):
                resp.close()

    def f():
        repo = HF_REPO.match(str(url or ""))
        if repo:
            with suppress(Exception):
                summary = _hf_model(repo.group(1), get)
                if summary:
                    return clip(summary)
        search = HF_SEARCH.match(str(url or ""))
        if search:
            with suppress(Exception):
                found = json.loads(get(f"https://huggingface.co/api/models?search={search.group(1)}"
                                       "&sort=downloads&limit=15"))
                return clip("\n".join([f"[Hugging Face models matching '{urllib.parse.unquote_plus(search.group(1))}', "
                                       "most downloaded first; fetch_url https://huggingface.co/<id> for files and card]"]
                                      + [f"{m.get('id')}  downloads {m.get('downloads')}  likes {m.get('likes')}  "
                                         f"updated {str(m.get('lastModified') or m.get('createdAt'))[:10]}"
                                         for m in found]) if found else "No Hugging Face models found.")
        raw = get(url)
        if raw.startswith("Error: fetch"):
            return raw
        # JSON (API responses) doesn't go through HTML cleanup
        text = raw if raw.lstrip()[:1] in "[{" else _clean(raw)
        if len(text) > FETCH_MAX_CHARS:
            # Room for the marker: a result over the tool output limit was cut a second time.
            keep = FETCH_MAX_CHARS - 400
            head = keep * 2 // 3            # a page's end is more often links and references than its start
            full = save_full(text)
            return text[:head] + f"\n…[middle cut: {len(text)} chars total" + \
                (f", full text saved to {full}, read_file it" if full else "") + "]\n" + text[-(keep - head):]
        return text
    return await asyncio.to_thread(f)


def cancel(url):
    """Close an in-flight fetch: the turn was aborted, so this request must not keep hanging."""
    resp = OPEN.pop(str(url), None)
    if resp is None:
        return False
    with suppress(Exception):
        resp.close()
    return True
