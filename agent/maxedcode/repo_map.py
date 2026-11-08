"""Cached, ranked AST map of a Python repository for local models."""
from __future__ import annotations

import ast
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

from .config import HOME, SETTINGS
from .permissions import protected
from .util import SKIP_DIRS, hash_cwd, tok

DEFAULT_BUDGET = 1024
_CACHE_VERSION = 1
_MAX_FILE_BYTES = 1_000_000
_SKIP = SKIP_DIRS | {".git", ".hg", ".svn", "node_modules", ".venv", "venv"}
_ENTRY_PRIORITY = {"local_agent.py": 9.0, "__init__.py": 2.0, "config.py": 8.0,
                   "app.py": 7.0, "headless.py": 7.0, "permissions.py": 6.0,
                   "schemas.py": 5.0, "catalog.py": 5.0}


def _budget(value=None):
    raw = value if value is not None else SETTINGS.get("repo_map_tokens", DEFAULT_BUDGET)
    try:
        return max(0, min(8192, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET


def _parse_symbols(path: Path):
    """Parse a source file into stable compact symbol lines and root-module references."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return [], []
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return [f"! SyntaxError L{e.lineno or 1}: {e.msg}"], []
    except OSError:
        return [], []

    result = []
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module.split(".")[0])
    if imports:
        result.append("imports " + ", ".join(dict.fromkeys(imports[:8])))

    def visit(nodes, parent=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") and node.name not in ("__init__", "__main__"):
                    continue
                try:
                    args = ast.unparse(node.args)
                except Exception:
                    args = "..."
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                result.append(f"{prefix} {parent + '.' if parent else ''}{node.name}({args}) L{node.lineno}")
            elif isinstance(node, ast.ClassDef):
                bases = []
                for base in node.bases[:3]:
                    try:
                        bases.append(ast.unparse(base))
                    except Exception:
                        pass
                suffix = "(" + ", ".join(bases) + ")" if bases else ""
                result.append(f"class {parent + '.' if parent else ''}{node.name}{suffix} L{node.lineno}")
                visit(node.body, parent + node.name + ".")
    visit(tree.body)
    return result, sorted(set(imports))


def _symbols(path: Path, root: Path):
    """Backward-compatible symbol helper, implemented on the cached parser's logic."""
    del root
    return _parse_symbols(path)[0]


def _cache_path(root: Path):
    return HOME / "tags_cache" / hash_cwd(str(root)) / "tags.json"


def _read_cache(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") == _CACHE_VERSION and isinstance(raw.get("files"), dict):
            return raw["files"]
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _write_cache(path: Path, records: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="tags-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": _CACHE_VERSION, "files": records}, f,
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    except OSError:
        # Repository map remains useful if the cache directory is read-only.
        pass


def _iter_python_files(base: Path):
    files = []
    for current_name, dirs, names in os.walk(base):
        current = Path(current_name)
        dirs[:] = sorted(d for d in dirs if d not in _SKIP and not protected(str(current / d)))
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = current / name
            if protected(str(path)):
                continue
            try:
                st = path.stat()
                if st.st_size > _MAX_FILE_BYTES or not path.is_file():
                    continue
            except OSError:
                continue
            files.append((path, st.st_mtime_ns, st.st_size))
    return files


def _mentioned_text(messages):
    if not messages:
        return ""
    chunks = []
    for message in messages[-12:]:
        if not isinstance(message, dict):
            continue
        value = message.get("content", "")
        if isinstance(value, str):
            chunks.append(value.lower())
        elif isinstance(value, list):
            chunks.extend(str(part.get("text", "")).lower() for part in value
                          if isinstance(part, dict) and part.get("type") == "text")
    return "\n".join(chunks)


def _module_name(rel: str):
    return rel[:-3].replace("/", ".").replace("\\", ".").removesuffix(".__init__")


def build_repo_map(root=".", budget_tokens=None, messages=None):
    """Return a stable repo map whose rendered size is bounded by an estimated token budget.

    Per-file AST tags are reused until mtime/size changes. Ranking combines entry-point priors,
    reverse-import references (a small PageRank-like centrality signal), and paths/symbols
    mentioned in recent conversation. Protected and deleted paths never leak from the cache.
    """
    budget = _budget(budget_tokens)
    if budget < 32:
        return ""
    base = Path(root).resolve()
    found = _iter_python_files(base)
    if not found:
        return ""

    cache_file = _cache_path(base)
    old_cache = _read_cache(cache_file)
    records = {}
    by_rel = {}
    for path, mtime_ns, size in found:
        rel = path.relative_to(base).as_posix()
        cached = old_cache.get(rel, {})
        if cached.get("mtime_ns") == mtime_ns and cached.get("size") == size \
                and isinstance(cached.get("symbols"), list) and isinstance(cached.get("imports"), list):
            record = {"mtime_ns": mtime_ns, "size": size,
                      "symbols": cached["symbols"], "imports": cached["imports"]}
        else:
            symbols, imports = _parse_symbols(path)
            record = {"mtime_ns": mtime_ns, "size": size, "symbols": symbols, "imports": imports}
        records[rel] = record
        by_rel[rel] = record
    # Assign a relative module-name-to-path map so imports increase referenced-module scores.
    module_to_rel = {}
    for rel in by_rel:
        module = _module_name(rel)
        module_to_rel[module] = rel
        module_to_rel[module.rsplit(".", 1)[-1]] = rel
    refs = Counter()
    for rel, record in by_rel.items():
        for imported in record["imports"]:
            target = module_to_rel.get(imported)
            if target and target != rel:
                refs[target] += 1

    mentions = _mentioned_text(messages)
    ranked = []
    for rel, record in by_rel.items():
        path = base / rel
        score = _ENTRY_PRIORITY.get(path.name, 1.0)
        score += refs[rel] * 1.4
        if rel.lower() in mentions or path.name.lower() in mentions:
            score += 12.0
        symbols_text = " ".join(record["symbols"]).lower()
        basename = path.stem.lower()
        if len(basename) > 2 and re.search(r"\b" + re.escape(basename) + r"\b", mentions):
            score += 5.0
        if symbols_text:
            # Reward modules with useful public API, with a mild penalty for very broad files.
            public_count = sum(1 for line in record["symbols"] if line.startswith(("def ", "async def ", "class ")))
            score += min(3.0, public_count * 0.08) - max(0, public_count - 100) * 0.01
        ranked.append((score, rel, record))
    ranked.sort(key=lambda item: (-item[0], len(Path(item[1]).parts), item[1].lower()))

    # Token-aware admission: symbol lines are priced by the same estimator used by prompt compaction.
    header = f"<repo_map budget≈{budget} tokens root={base}>"
    end_tag = "</repo_map>"
    output = [header]
    used = tok(header + "\n" + end_tag)
    omitted = 0
    for _score, rel, record in ranked:
        if protected(str(base / rel)):
            continue
        header_line = f"{rel}:"
        symbols = [f"  {item}" for item in record["symbols"][:80]]
        candidate = [header_line, *symbols]
        candidate_tokens = sum(tok(line + "\n") for line in candidate)
        if used + candidate_tokens <= budget:
            output.extend(candidate)
            used += candidate_tokens
            continue
        omitted += 1
        # Fit some lines from an important file even when the complete entry does not fit.
        remaining = budget - used - tok(header_line + "\n") - tok(end_tag + "\n")
        if remaining >= 10 and used + tok(header_line + "\n") + tok(end_tag + "\n") < budget:
            output.append(header_line)
            used += tok(header_line + "\n")
            for item in symbols:
                cost = tok(item + "\n")
                if used + cost + tok(end_tag + "\n") > budget:
                    break
                output.append(item)
                used += cost
        # Skip large lower-ranked entries without allowing one enormous file to starve others.
        continue
    if omitted:
        marker = f"… ({omitted} file(s) omitted by repo-map token budget)"
        while output and used + tok(marker + "\n" + end_tag) > budget:
            if output[-1].startswith("  "):
                used -= tok(output.pop() + "\n")
            else:
                break
        if used + tok(marker + "\n" + end_tag) <= budget:
            output.append(marker)
    output.append(end_tag)
    rendered = "\n".join(output)
    # Enforce the budget against the actual serialized result, not a chars-per-token proxy.
    while output and tok("\n".join(output)) > budget and len(output) > 2:
        # Drop lower-priority tail lines/entries first. The outer tags always remain.
        if output[-2].startswith("  "):
            output.pop(-2)
        elif output[-2].startswith("… ("):
            output.pop(-2)
        else:
            output.pop(-2)
    rendered = "\n".join(output)
    if tok(rendered) > budget:
        _write_cache(cache_file, records)
        return ""
    _write_cache(cache_file, records)
    return rendered
