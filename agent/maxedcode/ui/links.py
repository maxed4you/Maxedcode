"""Clickable links in the chat: a bare address becomes a real link, and opening it always asks first."""
import re
import webbrowser

# Scheme-less addresses only count when they end in a known TLD: "util.py", "state.json" and
# "BEHAVIOR.md" must stay plain text. "sh", "in" and "app" are left out on purpose: install.sh,
# MANIFEST.in and self.app are far more common in an agent's answer than such domains.
TLD = ("com|net|org|info|biz|site|online|xyz|page|cloud|tech|tools|work|space|link|click|fun|store|dev|me|co|io|"
       "ru|eu|uk|de|fr|it|nl|pl|cz|se|no|fi|dk|at|be|ch|jp|cn|kr|br|au|ua|by|kz|gg|to|tv|ws|ly")
SCHEME = r"(?:https?|ftp)"
URL = re.compile(
    SCHEME + r"://[^\s<>\"'\[\]]+"
    r"|www\.[\w-]+(?:\.[\w-]+)+[^\s<>\"'\[\]]*"
    # The TLD must end the name: logging.information and self.app.notify are not addresses.
    r"|(?<![\w./@\\-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:" + TLD + r")(?![\w-]|\.\w)"
    r"(?::\d{2,5})?(?:/[^\s<>\"'\[\]]*)?",
    re.I)
# Markdown that must not be touched: code fences, inline code, [text](url) and <autolink>.
# A fence still being streamed has no closing line yet: it runs to the end of the text.
SKIP = re.compile(r"```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]*`|\[[^\]]*\]\([^)\n]*\)|<[^<>\n]+>", re.S)
# A link linkify() made: [address](<https://address>).
MADE = re.compile(r"\[([^\]\n]+)\]\(<([^<>\n]+)>\)")
PLAIN = re.compile(r"(?:[A-Za-z0-9-]+\.)+(?:" + TLD + r")(?::\d{2,5})?(?:/\S*)?", re.I)


def trim(u):
    """Drop sentence punctuation at the end, but keep a closing bracket that belongs to the address."""
    u = u.rstrip(".,;:!?»“”'\"")
    while u.endswith(")") and u.count("(") < u.count(")"):
        u = u[:-1]
    return u


def clean_url(url):
    """Openable address, or None. A scheme-less address gets https://; anything that is not
    http/ftp stays plain text (we never hand a random address to the browser)."""
    u = trim((url or "").strip().strip("<>"))
    if not u:
        return None
    if u.lower().startswith(("http://", "https://", "ftp://")):
        return u
    return "https://" + u if PLAIN.fullmatch(u) else None


def linkify(md):
    """Wrap bare addresses in markdown links so Rich draws them as links (underline plus a link
    style the click handler reads). Existing links, code fences and inline code are left alone."""
    def repl(m):
        u = clean_url(m.group(0))
        return f"[{m.group(0)}](<{u}>)" if u else m.group(0)
    out, at = [], 0
    for block in SKIP.finditer(md or ""):
        out.append(URL.sub(repl, md[at:block.start()]))
        out.append(block.group(0))
        at = block.end()
    out.append(URL.sub(repl, md[at:]))
    return "".join(out)


# A fence line (``` or ~~~, up to 3 spaces in) and a table row: keep_breaks() leaves them as written.
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
TABLE_ROW = re.compile(r"\s*\|")
BREAK = "  "          # Markdown hard break: two spaces at the end of a line


def keep_breaks(md):
    """Keep the model's line breaks. Markdown joins the lines of one paragraph, so commands written one per
    line without a code fence came out as one line ("Get-Process Get-Service cd C:\\x"). Every line that
    another line follows gets a hard break; fenced code and table rows stay as written."""
    lines, out, fence = (md or "").split("\n"), [], None
    for i, line in enumerate(lines):
        m = FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1).startswith(fence):
                fence = None
        elif (fence is None and line.strip() and i + 1 < len(lines) and lines[i + 1].strip()
              and not line.endswith(BREAK) and not TABLE_ROW.match(line)):
            line += BREAK
        out.append(line)
    return "\n".join(out)


def chat_md(text):
    """An answer as the chat draws it: the model's line breaks kept, bare addresses as links."""
    return linkify(keep_breaks(text))


def unlinkify(md):
    """Undo chat_md() for copying: the answer is copied as the agent wrote it, without the
    [address](<address>) wrapping and the added line breaks. Links whose text is not the address itself stay."""
    out, fence = [], None
    lines = (md or "").split("\n")
    for i, line in enumerate(lines):
        m = FENCE.match(line)
        if m:
            fence = m.group(1) if fence is None else (None if m.group(1).startswith(fence) else fence)
        elif fence is None and i + 1 < len(lines) and line.endswith(BREAK):
            line = line[:-len(BREAK)]
        out.append(line)
    md = "\n".join(out)
    return MADE.sub(lambda m: m.group(1) if clean_url(m.group(1)) == m.group(2) else m.group(0), md)


def open_url(url):
    """Hand the address to the desktop browser. False when the browser refused. On Windows
    webbrowser.open is os.startfile, so only an http(s)/ftp address ever gets there."""
    url = clean_url(url)
    if not url:
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False
