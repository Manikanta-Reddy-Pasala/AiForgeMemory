"""Stage 4 — tree-sitter walk.

For every source file under repo_path:
    - hash the bytes
    - parse with tree-sitter (per-language grammar via tree-sitter-language-pack)
    - run the language-specific query (`queries/<lang>.scm`)
    - emit File_v2 props + Symbol_v2 nodes + DEFINES + IMPORTS edges (lists)

This module returns dataclasses; the writer (`store/symbol_writer.py`)
upserts them into Neo4j. Stage 5 (edges.py) layers CALLS on top.

Languages supported in plan 3:
    .py    -> python
    .java  -> java
    .ts/.tsx -> typescript

Files in unsupported languages are emitted as bare File_v2 nodes
(hash + lang + lines, no symbols).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser


# Mapping file extension → tree-sitter language id
_EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
}

# Documentation extensions — no tree-sitter parse, but still walked +
# embedded so README/CLAUDE.md/ADRs/CHANGELOG end up in Chunk_v2 and
# vector search can hit them.
_DOC_EXT: dict[str, str] = {
    ".md":   "doc-md",
    ".rst":  "doc-rst",
    ".adoc": "doc-adoc",
    ".txt":  "doc-txt",
}

# Skip these directories when walking — don't index build artifacts.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", ".DS_Store",
    ".aiforge", ".aiforge-worktrees", "graphify-out",
}


@dataclass
class WalkedSymbol:
    fqname: str
    kind: str               # class | interface | enum | annotation | method | function | field
    file_path: str
    signature: str = ""
    doc_first_line: str = ""
    line_start: int = 0
    line_end: int = 0


@dataclass
class WalkedFile:
    repo: str
    path: str               # repo-relative
    hash: str
    lang: str
    lines: int
    symbols: list[WalkedSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parse_error: bool = False


def lang_for(path: str | Path) -> str | None:
    suf = Path(path).suffix.lower()
    return _EXT_LANG.get(suf) or _DOC_EXT.get(suf)


def is_doc(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _DOC_EXT


def walk_repo(repo_path: str | Path, *, repo: str) -> list[WalkedFile]:
    repo_path = Path(repo_path).resolve()
    out: list[WalkedFile] = []
    for path in _iter_source_files(repo_path):
        rel = str(path.relative_to(repo_path))
        suf = Path(rel).suffix.lower()
        lang = _EXT_LANG.get(suf) or _DOC_EXT.get(suf)
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        sha = hashlib.sha256(data).hexdigest()
        lines = data.count(b"\n") + 1
        wf = WalkedFile(repo=repo, path=rel, hash=sha,
                        lang=lang or "other", lines=lines)
        # Code → tree-sitter parse for symbols/imports.
        # Docs → no parse; just walk so the embedder picks them up.
        if suf in _EXT_LANG:
            try:
                _parse_into(wf, data, lang)
            except Exception:
                wf.parse_error = True
        out.append(wf)
    return out


def _iter_source_files(root: Path):
    for p in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in _EXT_LANG or suf in _DOC_EXT:
            yield p


def _parse_into(wf: WalkedFile, source: bytes, lang: str) -> None:
    parser = get_parser(lang)
    language = get_language(lang)
    tree = parser.parse(source)
    query_text = _load_query(lang)
    if not query_text:
        return
    query = Query(language, query_text)
    cursor = QueryCursor(query)

    classes: list[tuple[str, object]] = []
    interfaces: list[tuple[str, object]] = []
    enums: list[tuple[str, object]] = []
    annotations: list[tuple[str, object]] = []
    fields: list[tuple[str, object]] = []
    methods: list[tuple[str, object]] = []
    functions: list[tuple[str, object]] = []
    imports: list[str] = []

    # `matches()` preserves per-pattern groupings, so name + def of the
    # same match always belong to the same node — robust against the
    # capture-ordering quirks of `captures()`.
    for _match_id, caps in cursor.matches(tree.root_node):
        # caps: dict[capture_name, list[Node]]
        if "class.def" in caps and "class.name" in caps:
            for d, n in zip(caps["class.def"], caps["class.name"]):
                classes.append((_text(n, source), d))
        elif "interface.def" in caps and "interface.name" in caps:
            for d, n in zip(caps["interface.def"], caps["interface.name"]):
                interfaces.append((_text(n, source), d))
        elif "enum.def" in caps and "enum.name" in caps:
            for d, n in zip(caps["enum.def"], caps["enum.name"]):
                enums.append((_text(n, source), d))
        elif "annotation.def" in caps and "annotation.name" in caps:
            for d, n in zip(caps["annotation.def"], caps["annotation.name"]):
                annotations.append((_text(n, source), d))
        elif "field.def" in caps and "field.name" in caps:
            for d, n in zip(caps["field.def"], caps["field.name"]):
                fields.append((_text(n, source), d))
        elif "method.def" in caps and "method.name" in caps:
            for d, n in zip(caps["method.def"], caps["method.name"]):
                methods.append((_text(n, source), d))
        elif "function.def" in caps and "function.name" in caps:
            for d, n in zip(caps["function.def"], caps["function.name"]):
                functions.append((_text(n, source), d))
        elif "import.module" in caps:
            for n in caps["import.module"]:
                imports.append(_text(n, source))
        elif "import.from" in caps:
            for n in caps["import.from"]:
                imports.append(_text(n, source))

    # Build owning-type index (class | interface | enum | annotation)
    # by line ranges so we can attach methods/fields back to their owner.
    type_ranges: list[tuple[int, int, str]] = []
    for cname, cdef in classes:
        type_ranges.append((cdef.start_point[0], cdef.end_point[0], cname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=cname, kind="class", node=cdef, source=source,
        ))
    for iname, idef in interfaces:
        type_ranges.append((idef.start_point[0], idef.end_point[0], iname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=iname, kind="interface", node=idef, source=source,
        ))
    for ename, edef in enums:
        type_ranges.append((edef.start_point[0], edef.end_point[0], ename))
        wf.symbols.append(_make_symbol(
            wf=wf, name=ename, kind="enum", node=edef, source=source,
        ))
    for aname, adef in annotations:
        type_ranges.append((adef.start_point[0], adef.end_point[0], aname))
        wf.symbols.append(_make_symbol(
            wf=wf, name=aname, kind="annotation", node=adef, source=source,
        ))
    for mname, mdef in methods:
        owner = _enclosing_class(type_ranges, mdef.start_point[0])
        fqname = _fqname(wf.path, mname, parent_class=owner)
        wf.symbols.append(_make_symbol(
            wf=wf, name=mname, kind="method", node=mdef, source=source,
            fqname=fqname,
        ))
    for fname_, fdef_ in fields:
        owner = _enclosing_class(type_ranges, fdef_.start_point[0])
        fqname = _fqname(wf.path, fname_, parent_class=owner)
        wf.symbols.append(_make_symbol(
            wf=wf, name=fname_, kind="field", node=fdef_, source=source,
            fqname=fqname,
        ))
    for fname, fdef in functions:
        # Skip if it's actually a method (already handled)
        if _enclosing_class(type_ranges, fdef.start_point[0]):
            continue
        wf.symbols.append(_make_symbol(
            wf=wf, name=fname, kind="function", node=fdef, source=source,
        ))

    wf.imports = list(dict.fromkeys(imports))   # de-dup, preserve order


def _enclosing_class(
    class_ranges: list[tuple[int, int, str]], line: int,
) -> str | None:
    for start, end, name in class_ranges:
        if start <= line <= end:
            return name
    return None


def _fqname(file_path: str, name: str, *, parent_class: str | None = None) -> str:
    if parent_class:
        return f"{file_path}::{parent_class}::{name}"
    return f"{file_path}::{name}"


def _make_symbol(
    *, wf: WalkedFile, name: str, kind: str, node, source: bytes,
    fqname: str | None = None,
) -> WalkedSymbol:
    sig_line = source.split(b"\n")[node.start_point[0]].decode(
        "utf-8", errors="replace"
    ).strip()
    return WalkedSymbol(
        fqname=fqname or _fqname(wf.path, name),
        kind=kind,
        file_path=wf.path,
        signature=sig_line[:200],
        doc_first_line="",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
    )


def _text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _load_query(lang: str) -> str:
    qfile = Path(__file__).parent / "queries" / f"{lang}.scm"
    if not qfile.is_file():
        # tsx maps to typescript query
        if lang == "tsx":
            qfile = Path(__file__).parent / "queries" / "typescript.scm"
        elif lang == "javascript":
            qfile = Path(__file__).parent / "queries" / "typescript.scm"
        if not qfile.is_file():
            return ""
    return qfile.read_text()
