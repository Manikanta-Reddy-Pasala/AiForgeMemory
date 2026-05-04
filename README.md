# AiForgeMemory

KISS code memory layer for AiForge agents. Tree-sitter ingest → Neo4j graph → unified search/recall over symbols, files, services, chunks, and free-form memory facts.

```
aiforge_memory/
├── core/        Neo4j driver + schema + ingest state
├── features/    one folder per feature: extract → store → tests
│   ├── repo, service, file, symbol, chunk, memory
│   ├── link, delta, scheduler, lsp, git_meta
│   ├── flow, eval
├── query/       cross-feature read paths (translator, bundle, fastpath)
├── api/         CLI (cli.py) + HTTP read endpoints (http.py)
├── ui/          FastAPI UI server + index.html (Repos / Memory / Search / Links / Graph / Scheduler / Health tabs)
└── ops/         backup, health
```

## Install

```bash
uv sync
```

## CLI

```bash
aiforge-memory ingest <repo>           # full ingest (tree-sitter walk → Neo4j)
aiforge-memory schedule run            # periodic ingest daemon
aiforge-memory remember <text>         # write a memory fact
aiforge-memory recall <query>          # vector recall
aiforge-memory ui --host 0.0.0.0 --port 8767
aiforge-memory health                  # probe Neo4j + LM + sidecars
```

Full list: `aiforge-memory --help`.

## HTTP / UI

`aiforge-memory ui` serves the read-only web UI on http://host:8767.

| Route | Purpose |
|---|---|
| `GET /api/repos` | indexed repo list |
| `GET /api/repo/{name}` | per-repo summary |
| `GET /api/file?path=...` | file content + symbols |
| `POST /api/search` | hybrid vector + fulltext + graph-hop |
| `GET /api/memory` | list memory facts |
| `GET /api/links` | cross-repo CALLS_REPO edges |
| `GET /api/scheduler` | scheduled ingest state |
| `GET /api/health` | Neo4j + sidecar probes |
| `GET /api/graphify` | repos with graphify-out + metadata |
| `GET /api/graphify/{repo}/report` | GRAPH_REPORT.md |
| `GET /api/graphify/{repo}/graph` | graph.json |
| `GET /graphify/{repo}/wiki/{path}` | inline wiki tree |

## Adding a feature

1. Create `aiforge_memory/features/<name>/` with `extract.py`, `store.py`, `tests/`.
2. Read from `core/neo4j.py` (driver) and `core/state.py` (ingest cursor).
3. Wire CLI entry in `api/cli.py` if user-facing.
4. Add HTTP endpoint in `api/http.py` or `ui/server.py` if browsable.

## Tests

```bash
uv run pytest aiforge_memory/ -q
```

160 unit tests + 34 Neo4j-gated integration tests. Skipped tests need a live Neo4j at `bolt://127.0.0.1:7687`.
