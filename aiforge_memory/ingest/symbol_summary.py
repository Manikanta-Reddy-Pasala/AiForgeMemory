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

# Cap body size sent to the LLM. mlx-lm 0.31 wedges on long prompts
# under sustained load; keep these conservative.
BODY_HEAD_LINES = int(os.environ.get("AIFORGE_SYMSUM_HEAD_LINES", "30"))
BODY_TAIL_LINES = int(os.environ.get("AIFORGE_SYMSUM_TAIL_LINES", "5"))
# Throttle: wait between successive LLM calls so mlx-lm has time to
# release internal state. 0.0 = no throttle.
INTER_CALL_DELAY_S = float(os.environ.get(
    "AIFORGE_SYMSUM_THROTTLE_S", "1.0",
))
# Per-request: timeout + retry. mlx-lm sometimes resets first SYN
# under load — one quick retry recovers most of those.
REQUEST_TIMEOUT_S = float(os.environ.get(
    "AIFORGE_SYMSUM_TIMEOUT_S", "120.0",
))
RETRY_MAX = int(os.environ.get("AIFORGE_SYMSUM_RETRY_MAX", "1"))
RETRY_BACKOFF_S = float(os.environ.get(
    "AIFORGE_SYMSUM_RETRY_BACKOFF_S", "3.0",
))
# Circuit breaker — if N consecutive calls fail, abort the whole run
# rather than burning 2000+ requests against a dead server.
ABORT_AFTER_CONSECUTIVE_ERRORS = int(os.environ.get(
    "AIFORGE_SYMSUM_ABORT_AFTER", "8",
))


class SymbolSummaryAborted(RuntimeError):
    """Raised when the LLM is failing too consistently to continue."""


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

    import time as _time

    out: list[SymbolSummary] = []
    total = len(candidates)
    consecutive_errors = 0
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

        # Circuit breaker — abort if the LLM is dead, rather than
        # burning the rest of the candidate list.
        if ss.skipped_reason == "llm_error":
            consecutive_errors += 1
        else:
            consecutive_errors = 0
        if consecutive_errors >= ABORT_AFTER_CONSECUTIVE_ERRORS:
            if on_each is not None:
                try:
                    on_each(ss, idx + 1, total)
                except Exception:
                    pass
            raise SymbolSummaryAborted(
                f"{consecutive_errors} consecutive LLM errors — "
                "aborting; restart the LLM server and retry"
            )

        if on_each is not None:
            try:
                on_each(ss, idx + 1, total)
            except Exception:
                # Callback failure must NOT abort the outer loop —
                # losing one progress update is acceptable.
                pass
        if INTER_CALL_DELAY_S > 0 and idx + 1 < total:
            _time.sleep(INTER_CALL_DELAY_S)
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
# Match the FIRST `{"summary":"..."}` JSON object anywhere in the text —
# necessary when the model wraps the answer in a thinking dump.
_SUMMARY_JSON_RE = re.compile(
    r'\{\s*"summary"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
    re.DOTALL,
)


def _parse(raw: str) -> str | None:
    """Return the summary string, '' for trivial, or None on error.

    Strategies, in order:
      1. fence-stripped JSON parse
      2. balanced-brace fallback (first { to last })
      3. regex extract — finds {"summary":"..."} embedded inside a
         thinking-mode preamble
    """
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return str(obj.get("summary", "")).strip()
    except json.JSONDecodeError:
        pass
    # Balanced-brace fallback
    i = cleaned.find("{")
    j = cleaned.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(cleaned[i : j + 1])
            if isinstance(obj, dict):
                return str(obj.get("summary", "")).strip()
        except json.JSONDecodeError:
            pass
    # Regex extract
    m = _SUMMARY_JSON_RE.search(cleaned)
    if m:
        # Unescape JSON string escapes
        try:
            return json.loads('"' + m.group(1) + '"')
        except json.JSONDecodeError:
            return m.group(1).strip()
    return None


def _call_llm(
    *, body: str, signature: str, doc: str,
    lang: str, path: str, fqname: str,
) -> str:
    """Real LLM call. Direct httpx + fresh client per call — OpenAI SDK
    keep-alive behaviour mlx-lm 0.31 doesn't tolerate. Multi-message
    chats also wedge mlx-lm 0.31, so we fold the system rules into
    one user message.

    Bounded retry on transient transport errors (RetryMAX backed by
    AIFORGE_SYMSUM_RETRY_MAX). On 4xx-class errors we abort immediately.
    """
    import time as _time

    import httpx

    # Compact "system" instructions inline; the file at PROMPT_PATH is
    # the reference but here we keep the LLM-facing text tight to avoid
    # token-length triggers in mlx-lm.
    # /no_think prefix is the Qwen3 convention to suppress chain-of-thought
    # at the prompt level — chat_template_kwargs.enable_thinking is not
    # honoured by every LM Studio build.
    user = (
        "/no_think\n"
        "Summarise this method in ONE sentence (max 25 words, present "
        "tense, what it DOES - side effects, IO, control flow). "
        "Output STRICT JSON only: {\"summary\":\"...\"}. "
        "If trivial (getter/setter/delegate/DTO), output {\"summary\":\"\"}.\n"
        "---\n"
        f"Symbol: {fqname}\n"
        f"Lang: {lang}\n"
        f"Signature: {signature}\n"
        + (f"Doc: {doc}\n" if doc else "")
        + f"Body:\n{body}\n"
    )
    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        # Bumped from 120: thinking-mode Qwen3 needs headroom to either
        # emit the cot AND the JSON, or — when /no_think + enable_thinking
        # both fail — at least let the cot finish so we can pluck the JSON
        # from reasoning_content.
        "max_tokens": 1024,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    url = DEFAULT_LM_URL.rstrip("/") + "/chat/completions"
    api_key = os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio")

    last_exc: Exception | None = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Force Connection: close so mlx-lm sees one-shot requests.
        # httpx default keep-alive + connection pooling triggers
        # "Connection reset by peer" mid-stream on mlx-lm 0.31.
        "Connection": "close",
    }
    # Build the JSON body once so we can stream it as raw bytes — keeps
    # the request shape identical to a `curl --data` call.
    import json as _json

    raw_body = _json.dumps(payload).encode("utf-8")

    for attempt in range(RETRY_MAX + 1):
        try:
            # http2=False, no client reuse, no transport pool.
            with httpx.Client(
                timeout=REQUEST_TIMEOUT_S,
                http2=False,
                limits=httpx.Limits(max_keepalive_connections=0,
                                    max_connections=1),
            ) as c:
                r = c.post(url, content=raw_body, headers=headers)
            if 400 <= r.status_code < 500:
                r.raise_for_status()
            r.raise_for_status()
            doc_body = r.json()
            choices = doc_body.get("choices") or []
            if not choices:
                return ""
            msg = choices[0].get("message") or {}
            # Some Qwen3 thinking models leave content="" and put the
            # answer (which often still contains our JSON) in
            # reasoning_content. Try content first, fall back.
            return (msg.get("content") or "").strip() \
                   or (msg.get("reasoning_content") or "").strip()
        except (httpx.HTTPStatusError,):
            raise  # 4xx — don't retry
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= RETRY_MAX:
                raise
            _time.sleep(RETRY_BACKOFF_S)
    # Defensive — loop above always returns or raises.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("symbol_summary._call_llm: unreachable")
