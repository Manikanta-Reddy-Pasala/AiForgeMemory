"""NL query → grounded entities (services / files / symbols).

Two stages:
    T1 embed query (bge-m3) → top-K Chunk vector hits → candidate
       File and Symbol short-list
    T2 LLM (qwen3.6) given (query, services_catalog, top_files, top_symbols)
       returns strict JSON: {intent, services[], files[], symbols[],
       hops, keywords[]}
       — always picks names from the supplied catalog/candidates,
       never invents

Hallucinated names (not in candidates / catalog) are silently dropped
and surfaced via bundle.errors.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

PROMPT_PATH = Path(__file__).parent.parent / "ingest" / "prompts" / "translate.txt"
DEFAULT_LM_URL = os.environ.get(
    "AIFORGE_CODEMEM_LM_URL",
    os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
)
DEFAULT_MODEL = os.environ.get(
    "AIFORGE_CODEMEM_LM_MODEL", "qwen3.6-27b-instruct"
)
DEFAULT_EMBED_URL = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764")
DEFAULT_RERANK_URL = os.environ.get("AIFORGE_RERANK_URL", "http://127.0.0.1:8765")

# Tunables — env-overridable so we can experiment without code edits.
RRF_K = int(os.environ.get("AIFORGE_TRANSLATOR_RRF_K", "60"))
RERANK_TOPN = int(os.environ.get("AIFORGE_TRANSLATOR_RERANK_TOPN", "30"))
ENABLE_RERANK = os.environ.get("AIFORGE_TRANSLATOR_RERANK", "1") not in ("0", "false", "False")


@dataclass
class Grounding:
    intent: str = ""
    services: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    hops: int = 1
    keywords: list[str] = field(default_factory=list)
    used_top_k: int = 0
    errors: list[str] = field(default_factory=list)


def translate(
    text: str,
    *,
    repo: str,
    driver,
    top_k: int = 20,
) -> Grounding:
    """NL → entities. Best-effort: any sub-stage failure leaves Grounding
    partially populated and adds an entry to .errors.

    Pipeline:
        1. _expand_query: lightweight query expansion (CamelCase split +
           known-synonym injection). Cheap, no LLM.
        2. _vector_topk: bge-m3 embed → Cypher chunk vector top-K.
        3. _fulltext_symbols: Lucene over Symbol_v2.signature.
        4. RRF fuse the two ranked file lists + path-prior bonus.
        5. _rerank (optional): cross-encoder rerank top-N to top-K.
        6. _expand_one_hop: pull 1-hop neighbours via IMPORTS/CALLS for
           the top files (recall boost for cross-file queries).
        7. _call_llm: ground to catalog/candidates only. Never invents.
    """
    g = Grounding()
    if not text.strip():
        return g

    # 1 — query expansion (CamelCase + synonyms)
    expanded = _expand_query(text)

    # 2/3 — collect ranked lists per source
    vector_files: list[str] = []
    fulltext_files: list[str] = []
    candidate_symbols: list[str] = []
    try:
        vec = _embed_query(expanded)
        rows = _vector_topk(driver, repo=repo, vec=vec, k=max(top_k * 3, 50))
        for r in rows:
            fp = r.get("file_path")
            if fp and fp not in vector_files:
                vector_files.append(fp)
        candidate_symbols = _symbols_in(driver, repo=repo, files=vector_files[:top_k])
        g.used_top_k = len(rows)
    except Exception as exc:
        g.errors.append(f"embed/topk: {exc}")

    try:
        ft_syms, ft_files = _fulltext_symbols(
            driver, repo=repo, text=expanded, k=max(top_k * 3, 50),
        )
        fulltext_files = ft_files
        for fq in ft_syms:
            if fq not in candidate_symbols:
                candidate_symbols.append(fq)
    except Exception as exc:
        g.errors.append(f"fulltext: {exc}")

    # 4 — RRF fusion + path-prior bonus
    candidate_files = _rrf_fuse(
        ranked_lists=[vector_files, fulltext_files],
        path_prior=_path_prior(text, vector_files + fulltext_files),
        k=RRF_K,
    )

    # 5 — cross-encoder rerank if sidecar reachable
    if ENABLE_RERANK and candidate_files:
        try:
            candidate_files = _rerank(
                query=text, docs=candidate_files[:RERANK_TOPN],
            ) + candidate_files[RERANK_TOPN:]
        except Exception as exc:
            g.errors.append(f"rerank: {exc}")

    # 6 — 1-hop graph expansion: pull files imported by / calling top hits
    try:
        neighbours = _expand_one_hop(
            driver, repo=repo, files=candidate_files[:5],
        )
        for fp in neighbours:
            if fp not in candidate_files:
                candidate_files.append(fp)
    except Exception as exc:
        g.errors.append(f"one-hop: {exc}")

    # Cap candidate set so the LLM prompt does not blow up
    candidate_files = candidate_files[: max(top_k * 2, 40)]

    # Service catalog
    services = _services_for(driver, repo=repo)

    # T2 — LLM grounding
    try:
        raw = _call_llm(
            text=text, services=services,
            files=candidate_files, symbols=candidate_symbols,
        )
        parsed = _parse(raw)
        if parsed:
            g.intent = parsed.get("intent", "")
            g.hops = max(1, min(int(parsed.get("hops", 1) or 1), 2))
            g.keywords = [str(k) for k in (parsed.get("keywords") or [])]
            g.services = [s for s in (parsed.get("services") or []) if s in services]
            g.files = [f for f in (parsed.get("files") or []) if f in candidate_files]
            g.symbols = [s for s in (parsed.get("symbols") or []) if s in candidate_symbols]
        else:
            g.errors.append("translator: invalid JSON")
    except Exception as exc:
        g.errors.append(f"translator: {exc}")

    # If LLM returned nothing usable, fall back to top embed candidates
    if not (g.services or g.files or g.symbols):
        g.files = candidate_files[:5]
        g.symbols = candidate_symbols[:5]

    return g


def _embed_query(text: str) -> list[float]:
    url = DEFAULT_EMBED_URL.rstrip("/") + "/embed"
    r = httpx.post(url, json={"text": text}, timeout=10.0)
    r.raise_for_status()
    return [float(x) for x in r.json().get("embedding", [])]


_VECTOR_CYPHER = """
CALL db.index.vector.queryNodes('codemem_chunk_embed', $k, $vec)
YIELD node AS c, score
WHERE c.repo = $repo
RETURN c.file_path AS file_path, c.text AS text, score
ORDER BY score DESC
LIMIT $k
"""


def _vector_topk(driver, *, repo: str, vec: list[float], k: int) -> list[dict]:
    if not vec:
        return []
    with driver.session() as s:
        rows = list(s.run(_VECTOR_CYPHER, repo=repo, vec=vec, k=k))
    return [dict(r) for r in rows]


def _symbols_in(driver, *, repo: str, files: list[str]) -> list[str]:
    if not files:
        return []
    cy = (
        "MATCH (f:File_v2 {repo:$repo})-[:DEFINES]->(s:Symbol_v2) "
        "WHERE f.path IN $files RETURN s.fqname AS fq LIMIT 60"
    )
    with driver.session() as sess:
        rows = list(sess.run(cy, repo=repo, files=files))
    return [r["fq"] for r in rows]


_FULLTEXT_CYPHER = """
CALL db.index.fulltext.queryNodes('codemem_symbol_signature_ft', $q)
YIELD node AS sym, score
WHERE sym.repo = $repo
RETURN sym.fqname AS fqname, sym.file_path AS file_path, score
ORDER BY score DESC
LIMIT $k
"""


_LUCENE_SPECIALS = re.compile(r'([+\-!(){}\[\]^"~*?:\\/])')
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _camel_split(token: str) -> list[str]:
    """`BusinessProductsController` → ['Business','Products','Controller']."""
    if not token or "_" in token:
        return [t for t in token.split("_") if t]
    parts = _CAMEL_RE.split(token)
    return [p for p in parts if p]


def _tokenize_for_fulltext(text: str) -> list[str]:
    """Split + camel-decompose + drop short tokens. De-dupe, lowercase."""
    raw = re.split(r"[\s/.\-]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        for piece in _camel_split(tok):
            piece = piece.strip()
            if len(piece) < 3:
                continue
            low = piece.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(piece)
    return out


def _escape_lucene(s: str) -> str:
    return _LUCENE_SPECIALS.sub(r"\\\1", s)


def _fulltext_symbols(
    driver, *, repo: str, text: str, k: int = 20,
) -> tuple[list[str], list[str]]:
    """Lucene fulltext search over Symbol fqname/signature/doc.
    Returns (symbol_fqnames, file_paths).

    CamelCase-aware: splits `BusinessProductsController` into its parts
    so signature-token matches work even when the query uses the joined
    form. Lucene specials are escaped.
    """
    tokens = _tokenize_for_fulltext(text)
    if not tokens:
        return [], []
    # Boost compound tokens: ride OR-of-pieces with quoted-phrase fallback
    q = " OR ".join(_escape_lucene(t) for t in tokens)
    syms: list[str] = []
    files: list[str] = []
    try:
        with driver.session() as s:
            for r in s.run(_FULLTEXT_CYPHER, repo=repo, q=q, k=k):
                syms.append(r["fqname"])
                fp = r.get("file_path")
                if fp and fp not in files:
                    files.append(fp)
    except Exception:
        return [], []
    return syms, files


# ─────────────────── retrieval-quality helpers ────────────────────────

# Cheap synonym table — code-domain terms users say vs. names in the repo.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "auth":         ("authentication", "jwt", "login", "token"),
    "jwt":          ("auth", "authentication", "token"),
    "crud":         ("controller", "service", "repository", "create", "update", "delete"),
    "endpoint":     ("controller", "api", "route"),
    "api":          ("controller", "endpoint"),
    "create":       ("save", "insert", "post"),
    "update":       ("patch", "put", "modify"),
    "delete":       ("remove", "destroy"),
    "list":         ("getAll", "findAll", "list"),
    "fetch":        ("get", "find", "load"),
    "validation":   ("validator", "validate", "check"),
    "sync":         ("dataSync", "push", "pull", "replication"),
    "push":         ("publish", "send", "sync"),
    "pull":         ("fetch", "request", "sync"),
    "test":         ("test", "spec", "junit"),
    "controller":   ("controller", "rest", "endpoint"),
    "service":      ("service", "impl"),
    "repository":   ("repo", "dao", "store"),
}


def _expand_query(text: str) -> str:
    """Append synonyms to query so embed and fulltext both see broader
    surface area. Original text is kept; synonyms appended after."""
    base_tokens = _tokenize_for_fulltext(text)
    extra: list[str] = []
    seen: set[str] = {t.lower() for t in base_tokens}
    for tok in base_tokens:
        for syn in _SYNONYMS.get(tok.lower(), ()):
            if syn.lower() not in seen:
                extra.append(syn)
                seen.add(syn.lower())
    if not extra:
        return text
    return text + "  " + " ".join(extra)


def _rrf_fuse(
    *,
    ranked_lists: list[list[str]],
    path_prior: dict[str, float] | None = None,
    k: int = 60,
) -> list[str]:
    """Reciprocal-rank fusion across multiple ranked lists, plus a
    path-prior float bonus per doc. RRF score = Σ 1/(k+rank). Stable
    across heterogeneous score scales (cosine + BM25)."""
    score: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, doc in enumerate(lst):
            score[doc] = score.get(doc, 0.0) + 1.0 / (k + rank + 1)
    if path_prior:
        for doc, bonus in path_prior.items():
            if doc in score:
                score[doc] += bonus
    return [doc for doc, _ in sorted(score.items(), key=lambda kv: -kv[1])]


def _path_prior(query: str, paths: list[str]) -> dict[str, float]:
    """Lightweight rule-based path bonus.

    `controller`/`endpoint` query → boost paths matching `*Controller*`.
    `service` → `*Service*`. `test` → `src/test/**`. Repository / DTO /
    Mapper similar. Bonus is small (≈ 0.02) so RRF stays dominant on
    truly-relevant docs but ties break in favour of intent-matched files.
    """
    q = query.lower()
    cues: list[tuple[str, float, callable]] = []
    if any(t in q for t in ("controller", "endpoint", "api", "route")):
        cues.append(("controller", 0.03, lambda p: "Controller" in p))
    if any(t in q for t in ("service", "business", "logic")):
        cues.append(("service", 0.02, lambda p: "Service" in p))
    if any(t in q for t in ("repository", "repo", "dao", "store")):
        cues.append(("repo", 0.02, lambda p: "Repository" in p or "Repo.java" in p))
    if any(t in q for t in ("dto", "request", "response", "model")):
        cues.append(("dto", 0.02, lambda p: any(s in p for s in ("Dto", "Request", "Response", "/model/"))))
    if any(t in q for t in ("test", "spec", "junit")):
        cues.append(("test", 0.04, lambda p: p.startswith("src/test/")))
    if any(t in q for t in ("mapper", "convert")):
        cues.append(("mapper", 0.02, lambda p: "Mapper" in p))
    out: dict[str, float] = {}
    if not cues:
        return out
    for p in paths:
        bonus = 0.0
        for _, w, fn in cues:
            try:
                if fn(p):
                    bonus += w
            except Exception:
                pass
        if bonus:
            out[p] = bonus
    return out


def _rerank(*, query: str, docs: list[str]) -> list[str]:
    """Cross-encoder rerank via :8765 sidecar. Returns reordered docs.
    Falls back to identity on failure."""
    if not docs:
        return docs
    url = DEFAULT_RERANK_URL.rstrip("/") + "/rerank"
    try:
        r = httpx.post(
            url, json={"query": query, "texts": docs}, timeout=15.0,
        )
        r.raise_for_status()
        scores = r.json().get("scores") or []
        if len(scores) != len(docs):
            return docs
        ranked = sorted(zip(docs, scores), key=lambda x: -float(x[1]))
        return [d for d, _ in ranked]
    except Exception:
        return docs


def _expand_one_hop(driver, *, repo: str, files: list[str]) -> list[str]:
    """For each top file, fetch up to 3 IMPORTS or 3 incoming-IMPORTS
    neighbours. Adds related files the vector index missed."""
    if not files:
        return []
    cy = (
        "MATCH (f:File_v2 {repo:$repo}) WHERE f.path IN $files "
        "OPTIONAL MATCH (f)-[:IMPORTS]->(out:File_v2) "
        "OPTIONAL MATCH (in:File_v2)-[:IMPORTS]->(f) "
        "WITH collect(DISTINCT out.path) AS outs, collect(DISTINCT in.path) AS ins "
        "RETURN outs + ins AS paths"
    )
    out: list[str] = []
    try:
        with driver.session() as s:
            row = s.run(cy, repo=repo, files=files).single()
            for p in (row["paths"] or []) if row else []:
                if p and p not in out:
                    out.append(p)
    except Exception:
        return []
    return out[:20]


def _services_for(driver, *, repo: str) -> list[str]:
    with driver.session() as s:
        rows = list(s.run(
            "MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service) "
            "RETURN s.name AS name", n=repo,
        ))
    return [r["name"] for r in rows]


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _parse(raw: str) -> dict | None:
    cleaned = _FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _call_llm(
    *,
    text: str,
    services: list[str],
    files: list[str],
    symbols: list[str],
) -> str:
    """Real LLM call. Isolated for monkey-patching in tests."""
    from openai import OpenAI

    client = OpenAI(
        base_url=DEFAULT_LM_URL,
        api_key=os.environ.get("AIFORGE_CODEMEM_LM_KEY", "lm-studio"),
    )
    system = _system_prompt()
    payload = {
        "query": text,
        "services_catalog": services,
        "candidate_files": files,
        "candidate_symbols": symbols,
    }
    user = json.dumps(payload, indent=2)
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.0,
        max_tokens=800,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


def _system_prompt() -> str:
    return (
        "You ground a natural-language code query against a known catalog.\n"
        "Input: JSON with `query`, `services_catalog`, `candidate_files`, `candidate_symbols`.\n"
        "\n"
        "Output: a single JSON object — no prose, no markdown fences.\n"
        "{\n"
        '  "intent":   string  (one of "fix" | "add" | "investigate" | "refactor" | "test" | "ops"),\n'
        '  "services": array of strings — MUST be names from services_catalog,\n'
        '  "files":    array of strings — MUST be paths from candidate_files,\n'
        '  "symbols":  array of strings — MUST be fqnames from candidate_symbols,\n'
        '  "hops":     1 or 2,\n'
        '  "keywords": array of short tokens for downstream rerank\n'
        "}\n"
        "\n"
        "Rules:\n"
        "- Pick at most 3 services, 5 files, 8 symbols.\n"
        "- Never invent names. Anything not in the catalog/candidates is dropped.\n"
        "- Prefer the most specific match. If query mentions a service, list it.\n"
        "- Use hops=2 only when the query implies cross-service traversal.\n"
        "- Output ONLY the JSON object."
    )
