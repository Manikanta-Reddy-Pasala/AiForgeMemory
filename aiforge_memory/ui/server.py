"""Minimal read-only UI for AiForgeMemory.

Single-file FastAPI server + embedded HTML/JS. No auth — bind to
localhost or trusted LAN. Five things visible:

    /                  Dashboard (repos, scheduler, health)
    /api/repos         List repos + counts
    /api/health        Sidecar status (calls ops.health)
    /api/scheduler     Daemon + per-repo last_run/status
    /api/search        NL search → ContextBundle (any repo)
    /api/memory        Memory nodes (decision/observation/note/doc)
    /api/repo/{name}   Repo detail (recent files + summaries)
    /api/file          File detail by path (chunks + symbols)

Run:
    aiforge-memory ui [--host 0.0.0.0] [--port 8767]

Requires fastapi + uvicorn (added to optional `[ui]` extra).
"""
from __future__ import annotations

import os
from pathlib import Path

# Lazy-import fastapi so the rest of the package works without it.
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, JSONResponse
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


HTML_PATH = Path(__file__).parent / "index.html"


def build_app():
    if not _HAS_FASTAPI:
        raise RuntimeError(
            "fastapi not installed. Run: uv pip install '.[ui]'"
        )

    from neo4j import GraphDatabase

    app = FastAPI(title="AiForgeMemory UI", docs_url="/docs", redoc_url=None)

    def _driver():
        uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
        user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
        pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
        return GraphDatabase.driver(uri, auth=(user, pw))

    # ── HTML ─────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        if HTML_PATH.is_file():
            return HTML_PATH.read_text()
        return "<h1>AiForgeMemory UI</h1><p>HTML missing.</p>"

    # ── Repos list with counts ───────────────────────────────────────

    @app.get("/api/repos")
    async def repos():
        """Per-repo counts via separate queries — avoids the cartesian
        explosion that an OPTIONAL MATCH chain produces on PCB-scale graphs."""
        drv = _driver()
        try:
            with drv.session() as s:
                meta = list(s.run("""
                    MATCH (r:Repo)
                    RETURN r.name AS name,
                           coalesce(r.lang_primary,'') AS lang,
                           coalesce(r.head_sha,'') AS head,
                           coalesce(r.branch,'') AS branch,
                           toString(r.last_indexed_at) AS indexed
                    ORDER BY r.name
                """))
                out: list[dict] = []
                for m in meta:
                    n = m["name"]
                    f = s.run(
                        "MATCH (f:File_v2 {repo:$n}) RETURN count(f) AS c", n=n,
                    ).single()["c"]
                    sym = s.run(
                        "MATCH (s:Symbol_v2 {repo:$n}) RETURN count(s) AS c", n=n,
                    ).single()["c"]
                    ch = s.run(
                        "MATCH (c:Chunk_v2 {repo:$n}) RETURN count(c) AS c", n=n,
                    ).single()["c"]
                    out.append({
                        **dict(m),
                        "files": f, "symbols": sym, "chunks": ch,
                    })
            return out
        finally:
            drv.close()

    @app.get("/api/repo/{name}")
    async def repo_detail(name: str):
        drv = _driver()
        try:
            with drv.session() as s:
                row = s.run(
                    "MATCH (r:Repo {name:$n}) RETURN r", n=name,
                ).single()
                if not row:
                    raise HTTPException(404, "repo not found")
                meta = dict(row["r"])
                # cast datetime fields to string
                for k, v in list(meta.items()):
                    if hasattr(v, "iso_format"):
                        meta[k] = str(v)
                # NULLS LAST is Cypher 25-only; older Neo4j (<5.27) refuses.
                # Coerce nulls to a fixed-old datetime via coalesce so the
                # sort works on every supported version.
                files = [dict(r) for r in s.run("""
                    MATCH (f:File_v2 {repo:$n})
                    RETURN f.path AS path, f.lang AS lang, f.lines AS lines,
                           coalesce(f.summary,'') AS summary,
                           toString(f.indexed_at) AS indexed
                    ORDER BY coalesce(f.indexed_at, datetime('1970-01-01T00:00:00Z')) DESC, f.path
                    LIMIT 200
                """, n=name)]
                services = [dict(r) for r in s.run("""
                    MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service)
                    RETURN s.name AS name, s.role AS role,
                           s.description AS description, s.port AS port,
                           s.tech_stack AS tech_stack
                """, n=name)]
            return {"meta": meta, "services": services, "files": files}
        finally:
            drv.close()

    @app.get("/api/file")
    async def file_detail(repo: str, path: str):
        drv = _driver()
        try:
            with drv.session() as s:
                row = s.run("""
                    MATCH (f:File_v2 {repo:$r, path:$p}) RETURN f
                """, r=repo, p=path).single()
                if not row:
                    raise HTTPException(404, "file not found")
                meta = dict(row["f"])
                for k, v in list(meta.items()):
                    if hasattr(v, "iso_format"):
                        meta[k] = str(v)
                symbols = [dict(r) for r in s.run("""
                    MATCH (f:File_v2 {repo:$r, path:$p})-[:DEFINES]->(s:Symbol_v2)
                    RETURN s.fqname AS fqname, s.kind AS kind,
                           s.signature AS signature,
                           coalesce(s.visibility,'') AS visibility,
                           coalesce(s.return_type,'') AS return_type,
                           s.line_start AS line_start, s.line_end AS line_end
                    ORDER BY s.line_start
                """, r=repo, p=path)]
                chunks = [dict(r) for r in s.run("""
                    MATCH (f:File_v2 {repo:$r, path:$p})-[:CHUNKED_AS]->(c:Chunk_v2)
                    RETURN c.id AS id, c.line_start AS line_start,
                           c.line_end AS line_end, c.token_count AS tokens,
                           substring(c.text, 0, 400) AS preview
                    ORDER BY c.line_start
                """, r=repo, p=path)]
            return {"meta": meta, "symbols": symbols, "chunks": chunks}
        finally:
            drv.close()

    # ── Scheduler + health ───────────────────────────────────────────

    @app.get("/api/scheduler")
    async def scheduler_status():
        from aiforge_memory.ingest import scheduler as sched
        return JSONResponse(sched.daemon_status())

    @app.get("/api/health")
    async def health():
        from dataclasses import asdict

        from aiforge_memory.ops import health as h
        report = h.check_all()
        return {
            "ts": report.ts,
            "overall_ok": report.overall_ok,
            "checks": [asdict(c) for c in report.checks],
        }

    # ── Search (NL → ContextBundle) ──────────────────────────────────

    @app.post("/api/search")
    async def search(payload: dict):
        from dataclasses import asdict

        from aiforge_memory.query import bundle as B
        text = (payload.get("query") or "").strip()
        repo = (payload.get("repo") or "").strip()
        if not text or not repo:
            raise HTTPException(400, "query + repo required")
        token_budget = int(payload.get("token_budget") or 4000)
        drv = _driver()
        try:
            b = B.query(text, repo=repo, driver=drv, token_budget=token_budget)
            return {
                "query": text, "repo": repo,
                "bundle": asdict(b),
                "rendered": b.render(),
            }
        finally:
            drv.close()

    # ── Memory nodes ─────────────────────────────────────────────────

    @app.get("/api/memory")
    async def memory_list(
        repo: str,
        type: str | None = Query(None,
                                 pattern="^(decision|observation|note|doc)$"),
        limit: int = 50,
    ):
        from aiforge_memory.store import memory_writer
        label_map = {
            "decision": "Decision_v2", "observation": "Observation_v2",
            "note": "Note_v2", "doc": "Doc_v2",
        }
        label = label_map.get(type) if type else None
        drv = _driver()
        try:
            return memory_writer.list_memory(
                drv, repo=repo, label=label, limit=limit,
            )
        finally:
            drv.close()

    # ── Cross-repo ───────────────────────────────────────────────────

    @app.get("/api/links")
    async def links(repo: str | None = None):
        from aiforge_memory.store import link_writer
        drv = _driver()
        try:
            return link_writer.list_edges(drv, repo=repo)
        finally:
            drv.close()

    return app


def serve(host: str = "127.0.0.1", port: int = 8767) -> None:
    """Run the UI server. Blocking."""
    if not _HAS_FASTAPI:
        raise RuntimeError(
            "fastapi/uvicorn not installed. Run: uv pip install '.[ui]'"
        )
    import uvicorn
    uvicorn.run(build_app(), host=host, port=port, log_level="warning")
