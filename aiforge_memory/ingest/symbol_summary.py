"""Per-symbol LLM summarisation — Stage 6.5.

For non-trivial methods/functions in a repo, ask the LLM for a 1-line
behavior description (what it DOES, not what types it has). Result lands
on ``Symbol_v2.summary``.

Filters (tunable via env):
- kind in {method, function}                  AIFORGE_SYMSUM_KINDS
- line_count >= min_lines (default 8)         AIFORGE_SYMSUM_MIN_LINES
- file size <= MAX_FILE_BYTES                 AIFORGE_SYMSUM_MAX_FILE_BYTES
- skip body if no bytes between line_start
  and line_end
- skip getters / setters by signature shape

Soft contract:
- LLM bad JSON → keep prior summary, mark skipped_reason=llm_error
- LLM unreachable → skip silently, increment counter
- empty 'summary' from LLM → trivial method, leave Symbol_v2.summary unset
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from aiforge_memory.ingest.treesitter_walk import WalkedFile

PROMPT_PATH = Path(__file__).parent / "prompts" / "symbol_summary.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct",
)
MIN_LINES = int(os.environ.get("AIFORGE_SYMSUM_MIN_LINES", "8"))
MAX_FILE_BYTES = int(os.environ.get(
    "AIFORGE_SYMSUM_MAX_FILE_BYTES", "262144",
))
KINDS_RAW = os.environ.get("AIFORGE_SYMSUM_KINDS", "method,function")
ALLOWED_KINDS = {k.strip().lower() for k in KINDS_RAW.split(",") if k.strip()}

# Cap body size sent to the LLM — long methods get truncated to head+tail
# so the prompt stays bounded.
BODY_HEAD_LINES = 80
BODY_TAIL_LINES = 20


@dataclass
class SymbolSummary:
    repo: str
    fqname: str
    summary: str = ""
    skipped_reason: str = ""    # "" | "kind" | "too_short" | "too_large" | "llm_error" | "trivial"


# Heuristic: the signature of a getter/setter rarely has more than a
# return statement or assignment in its body. We additionally require
# a meaningful line span, but keep the regex as a cheap pre-filter.
_GETTER_SIG = re.compile(r"\b(get|is|has|set)[A-Z]\w*\s*\(")


def summarise_symbols(
    walked: list[WalkedFile],
    *,
    repo: str,
    repo_root: str | Path,
    limit: int | None = None,
    min_lines: int | None = None,
    on_each: "callable | None" = None,
) -> list[SymbolSummary]:
    """One LLM call per qualifying symbol. Order: largest body first
    so the most expensive things land in the budget.

    Args:
        limit: hard cap on LLM calls (None = unlimited)
        min_lines: override env MIN_LINES floor
        on_each: optional callback ``fn(summary: SymbolSummary, idx: int,
                 total: int) -> None`` invoked after each LLM response.
                 The CLI uses this to write incrementally + emit progress
                 instead of waiting for the whole batch.
    """
    repo_root = Path(repo_root)
    floor = MIN_LINES if min_lines is None else int(min_lines)

    # Collect candidates with their containing file content cached so
    # we don't read each file once per symbol.
    candidates: list[tuple[WalkedFile, object, int]] = []
    file_bytes_cache: dict[str, bytes] = {}
    for wf in walked:
        if wf.parse_error or not wf.symbols:
            continue
        try:
            buf = file_bytes_cache.get(wf.path)
            if buf is None:
                buf = (repo_root / wf.path).read_bytes()
                if len(buf) > MAX_FILE_BYTES:
                    continue
                file_bytes_cache[wf.path] = buf
        except OSError:
            continue
        for sym in wf.symbols:
            kind = (getattr(sym, "kind", "") or "").lower()
            if kind not in ALLOWED_KINDS:
                continue
            ls = getattr(sym, "line_start", 0) or 0
            le = getattr(sym, "line_end", 0) or 0
            n_lines = max(0, le - ls + 1)
            if n_lines < floor:
                continue
            if _GETTER_SIG.search(getattr(sym, "signature", "") or ""):
                # quick getter/setter skip — body is almost always trivial
                if n_lines < 3:
                    continue
            candidates.append((wf, sym, n_lines))

    # Largest first — gives the LLM budget the highest-value items first
    # in case `limit` cuts the tail.
    candidates.sort(key=lambda x: x[2], reverse=True)
    if limit is not None:
        candidates = candidates[:max(0, int(limit))]

    out: list[SymbolSummary] = []
    total = len(candidates)
    for idx, (wf, sym, _) in enumerate(candidates):
        ss = SymbolSummary(repo=repo, fqname=sym.fqname)
        body = _slice_body(
            file_bytes_cache[wf.path],
            sym.line_start, sym.line_end,
        )
        if not body.strip():
            ss.skipped_reason = "too_short"
        else:
            try:
                raw = _call_llm(
                    body=body, signature=sym.signature or "",
                    doc=getattr(sym, "doc_first_line", "") or "",
                    lang=wf.lang or "", path=wf.path,
                    fqname=sym.fqname,
                )
                parsed = _parse(raw)
                if parsed is None:
                    ss.skipped_reason = "llm_error"
                elif not parsed:
                    ss.skipped_reason = "trivial"
                else:
                    ss.summary = parsed
            except Exception:
                ss.skipped_reason = "llm_error"
        out.append(ss)
        if on_each is not None:
            try:
                on_each(ss, idx + 1, total)
            except Exception:
                # Callback failure must NOT abort the outer loop —
                # losing one progress update is acceptable.
                pass
    return out


def _slice_body(content: bytes, line_start: int, line_end: int) -> str:
    """Return UTF-8 slice for inclusive line range, head-tail truncated
    to keep prompts bounded. line_start/line_end are 1-based."""
    lines = content.decode("utf-8", errors="replace").splitlines()
    if line_start < 1:
        line_start = 1
    if line_end < line_start:
        line_end = line_start
    span = lines[line_start - 1: line_end]
    if len(span) > BODY_HEAD_LINES + BODY_TAIL_LINES:
        span = (
            span[:BODY_HEAD_LINES]
            + ["    // … truncated …"]
            + span[-BODY_TAIL_LINES:]
        )
    return "\n".join(span)


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse(raw: str) -> str | None:
    """Return the summary string, '' for trivial, or None on error."""
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: balanced-brace extract
        i = cleaned.find("{")
        j = cleaned.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(cleaned[i : j + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    s = str(obj.get("summary", "")).strip()
    return s


def _call_llm(
    *, body: str, signature: str, doc: str,
    lang: str, path: str, fqname: str,
) -> str:
    """Real LLM call. Isolated so tests can monkey-patch."""
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    system = PROMPT_PATH.read_text()
    user = (
        f"File: {path}\n"
        f"Language: {lang}\n"
        f"Symbol: {fqname}\n"
        f"Signature: {signature}\n"
        + (f"Doc: {doc}\n" if doc else "")
        + f"\nBody:\n{body}\n"
    )
    # mlx-lm 0.31 rejects response_format=json_object with conn-reset.
    # Drop it; the parser is already fence-tolerant + balanced-brace
    # fallback, which covers what local models actually emit.
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=120,
    )
    return resp.choices[0].message.content or ""
