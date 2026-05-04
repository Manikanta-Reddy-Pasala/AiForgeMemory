"""Cypher writer for the memory layer — Decision_v2, Observation_v2,
Note_v2, Doc_v2 + MENTIONS / SUPERSEDES / RECORDS edges.

Public surface:
    upsert_decision(driver, *, repo, **fields)        -> dict
    upsert_observation(driver, *, repo, **fields)     -> dict
    upsert_note(driver, *, repo, **fields)            -> dict
    upsert_doc(driver, *, repo, **fields)             -> dict
    forget(driver, *, repo, node_id, label)           -> dict
    list_memory(driver, *, repo, label=None, limit=50) -> list[dict]
    recall_observations(driver, *, repo, query_vec, k=10) -> list[dict]

Idempotent: keyed on caller-supplied id (uuid4 if omitted). Auto-stamps
created_at, updated_at, schema_version. References (refs=[fqname|path])
become MENTIONS edges to Symbol_v2 / File_v2 nodes if they exist.

Memory nodes are *additive* — they coexist with the code graph and can
be retrieved either directly (by id) or via vector recall over
Observation embeddings.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Iterable

_SCHEMA_VERSION = "codemem-v1"

_ALLOWED_LABELS = {"Decision_v2", "Observation_v2", "Note_v2", "Doc_v2"}


# ─── Decision ─────────────────────────────────────────────────────────

_UPSERT_DECISION = """
MERGE (d:Decision_v2 {id: $id})
ON CREATE SET d.created_at     = datetime({epochSeconds: toInteger($now)}),
              d.schema_version = $schema_version
SET d.repo        = $repo,
    d.title       = $title,
    d.body        = $body,
    d.rationale   = $rationale,
    d.status      = $status,
    d.author      = $author,
    d.session_id  = $session_id,
    d.tags        = $tags,
    d.tags_text   = $tags_text,
    d.updated_at  = datetime({epochSeconds: toInteger($now)})
WITH d
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(d)
RETURN d.id AS id
"""


def upsert_decision(
    driver,
    *,
    repo: str,
    title: str,
    body: str = "",
    rationale: str = "",
    status: str = "active",          # active | superseded | rejected
    author: str = "",
    session_id: str = "",
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    supersedes_id: str | None = None,
    id: str | None = None,
) -> dict:
    """Record a durable architectural / process decision."""
    nid = id or _new_id("dec")
    tags = list(tags or [])
    params = {
        "id": nid, "repo": repo, "title": title, "body": body,
        "rationale": rationale, "status": status, "author": author,
        "session_id": session_id, "tags": tags,
        "tags_text": " ".join(tags),
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_DECISION, **params).consume()
        _link_refs(s, repo=repo, src_label="Decision_v2", src_id=nid,
                   refs=refs or [])
        if supersedes_id:
            s.run(
                "MATCH (a:Decision_v2 {id:$a}), (b:Decision_v2 {id:$b}) "
                "MERGE (a)-[:SUPERSEDES]->(b) "
                "SET b.status = 'superseded', "
                "    b.updated_at = datetime({epochSeconds: toInteger($now)})",
                a=nid, b=supersedes_id, now=time.time(),
            ).consume()
    return {"id": nid, "label": "Decision_v2"}


# ─── Observation ──────────────────────────────────────────────────────

_UPSERT_OBSERVATION = """
MERGE (o:Observation_v2 {id: $id})
ON CREATE SET o.created_at     = datetime({epochSeconds: toInteger($now)}),
              o.schema_version = $schema_version
SET o.repo        = $repo,
    o.kind        = $kind,
    o.text        = $text,
    o.author      = $author,
    o.session_id  = $session_id,
    o.tags        = $tags,
    o.tags_text   = $tags_text,
    o.embed_vec   = $embed_vec,
    o.embed_model = $embed_model,
    o.updated_at  = datetime({epochSeconds: toInteger($now)})
WITH o
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(o)
RETURN o.id AS id
"""


def upsert_observation(
    driver,
    *,
    repo: str,
    text: str,
    kind: str = "note",              # note | bug | learning | gotcha | feedback
    author: str = "",
    session_id: str = "",
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    embed_vec: list[float] | None = None,
    embed_model: str = "bge-m3",
    id: str | None = None,
) -> dict:
    """Record an agent / human observation. Embed vector is optional —
    when supplied, vector recall over Observation_v2 becomes available."""
    nid = id or _new_id("obs")
    tags = list(tags or [])
    params = {
        "id": nid, "repo": repo, "kind": kind, "text": text,
        "author": author, "session_id": session_id, "tags": tags,
        "tags_text": " ".join(tags),
        "embed_vec": embed_vec, "embed_model": embed_model,
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_OBSERVATION, **params).consume()
        _link_refs(s, repo=repo, src_label="Observation_v2", src_id=nid,
                   refs=refs or [])
    return {"id": nid, "label": "Observation_v2"}


# ─── Note ─────────────────────────────────────────────────────────────

_UPSERT_NOTE = """
MERGE (n:Note_v2 {id: $id})
ON CREATE SET n.created_at     = datetime({epochSeconds: toInteger($now)}),
              n.schema_version = $schema_version
SET n.repo        = $repo,
    n.title       = $title,
    n.body        = $body,
    n.author      = $author,
    n.tags        = $tags,
    n.updated_at  = datetime({epochSeconds: toInteger($now)})
WITH n
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(n)
RETURN n.id AS id
"""


def upsert_note(
    driver,
    *,
    repo: str,
    title: str,
    body: str = "",
    author: str = "",
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    id: str | None = None,
) -> dict:
    nid = id or _new_id("note")
    params = {
        "id": nid, "repo": repo, "title": title, "body": body,
        "author": author, "tags": list(tags or []),
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_NOTE, **params).consume()
        _link_refs(s, repo=repo, src_label="Note_v2", src_id=nid,
                   refs=refs or [])
    return {"id": nid, "label": "Note_v2"}


# ─── Doc (web doc / external) ─────────────────────────────────────────

_UPSERT_DOC = """
MERGE (d:Doc_v2 {id: $id})
ON CREATE SET d.created_at     = datetime({epochSeconds: toInteger($now)}),
              d.schema_version = $schema_version
SET d.repo        = $repo,
    d.url         = $url,
    d.title       = $title,
    d.body        = $body,
    d.source_kind = $source_kind,
    d.fetched_at  = datetime({epochSeconds: toInteger($now)})
WITH d
MATCH (r:Repo {name: $repo})
MERGE (r)-[:RECORDS]->(d)
RETURN d.id AS id
"""


def upsert_doc(
    driver,
    *,
    repo: str,
    title: str,
    body: str,
    url: str = "",
    source_kind: str = "web",       # web | readme | runbook | api-spec
    refs: list[str] | None = None,
    id: str | None = None,
) -> dict:
    nid = id or _new_id("doc")
    params = {
        "id": nid, "repo": repo, "title": title, "body": body,
        "url": url, "source_kind": source_kind,
        "schema_version": _SCHEMA_VERSION, "now": time.time(),
    }
    with driver.session() as s:
        s.run(_UPSERT_DOC, **params).consume()
        _link_refs(s, repo=repo, src_label="Doc_v2", src_id=nid,
                   refs=refs or [])
    return {"id": nid, "label": "Doc_v2"}


# ─── Maintenance ──────────────────────────────────────────────────────

def forget(driver, *, repo: str, node_id: str, label: str) -> dict:
    """Hard-delete a memory node + its edges. ``label`` must be one of
    Decision_v2, Observation_v2, Note_v2, Doc_v2."""
    if label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    cy = (
        f"MATCH (n:{label} {{id:$id, repo:$repo}}) "
        "WITH n, n.id AS id DETACH DELETE n RETURN id"
    )
    with driver.session() as s:
        row = s.run(cy, id=node_id, repo=repo).single()
    return {"deleted": row["id"] if row else None}


def list_memory(
    driver, *, repo: str, label: str | None = None, limit: int = 50,
) -> list[dict]:
    """Return memory nodes for a repo, newest first."""
    if label and label not in _ALLOWED_LABELS:
        raise ValueError(f"unknown memory label: {label}")
    if label:
        cy = (
            f"MATCH (n:{label} {{repo:$repo}}) "
            "RETURN n.id AS id, labels(n)[0] AS label, "
            "       coalesce(n.title,'') AS title, "
            "       coalesce(n.text, n.body, '') AS text, "
            "       coalesce(n.kind, n.status, '') AS kind, "
            "       toString(n.created_at) AS created_at "
            "ORDER BY n.created_at DESC LIMIT $limit"
        )
    else:
        cy = (
            "MATCH (r:Repo {name:$repo})-[:RECORDS]->(n) "
            "WHERE any(l IN labels(n) WHERE l IN "
            "  ['Decision_v2','Observation_v2','Note_v2','Doc_v2']) "
            "RETURN n.id AS id, [l IN labels(n) WHERE l ENDS WITH '_v2'][0] AS label, "
            "       coalesce(n.title,'') AS title, "
            "       coalesce(n.text, n.body, '') AS text, "
            "       coalesce(n.kind, n.status, '') AS kind, "
            "       toString(n.created_at) AS created_at "
            "ORDER BY n.created_at DESC LIMIT $limit"
        )
    with driver.session() as s:
        return [dict(r) for r in s.run(cy, repo=repo, limit=limit)]


_RECALL_OBSERVATION = """
CALL db.index.vector.queryNodes('codemem_observation_embed', $k, $vec)
YIELD node AS o, score
WHERE o.repo = $repo
RETURN o.id AS id, o.text AS text, o.kind AS kind,
       coalesce(o.tags,[]) AS tags, score
ORDER BY score DESC LIMIT $k
"""


def recall_observations(
    driver, *, repo: str, query_vec: list[float], k: int = 10,
) -> list[dict]:
    if not query_vec:
        return []
    with driver.session() as s:
        return [dict(r) for r in s.run(
            _RECALL_OBSERVATION, repo=repo, vec=query_vec, k=k,
        )]


# ─── helpers ──────────────────────────────────────────────────────────

def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _link_refs(
    session, *, repo: str, src_label: str, src_id: str, refs: Iterable[str],
) -> None:
    """Create MENTIONS edges from the memory node to existing
    Symbol_v2 (matched by fqname) or File_v2 (matched by path).

    A ref string with `::` is treated as a symbol fqname; otherwise as a
    file path. Missing targets are silently ignored — no placeholders."""
    for ref in refs:
        ref = (ref or "").strip()
        if not ref:
            continue
        if "::" in ref:
            cy = (
                f"MATCH (src:{src_label} {{id:$sid}}), "
                "(t:Symbol_v2 {repo:$repo, fqname:$ref}) "
                "MERGE (src)-[:MENTIONS]->(t)"
            )
        else:
            cy = (
                f"MATCH (src:{src_label} {{id:$sid}}), "
                "(t:File_v2 {repo:$repo, path:$ref}) "
                "MERGE (src)-[:MENTIONS]->(t)"
            )
        session.run(cy, sid=src_id, repo=repo, ref=ref).consume()
