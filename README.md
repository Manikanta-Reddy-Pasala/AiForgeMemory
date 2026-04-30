# AiForgeMemory

Unified code memory backend for local-LLM dev tooling.

> Single read API for code context across `Repo` → `Service` → `File` → `Symbol`
> + chunk vectors. Repomix + tree-sitter + LLM grounding + Neo4j. Cursor-like
> retrieval that an autonomous coding agent can call without reasoning about
> backends.

## Why

Stitched 8-source aggregators feel high-tech but answer real queries poorly. AiForgeMemory replaces them with one coherent four-level model and one query function.

It's the extraction of the codemem subsystem from
[AIForgeCrew](https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew); the
design spec lives there: `docs/superpowers/specs/2026-04-30-unified-code-memory-design.md`.

## Data model (Neo4j 5)

```
   Repo  ─OWNS_SERVICE─►  Service  ─CONTAINS_FILE─►  File_v2 ─DEFINES─►  Symbol_v2
    │                                                    │                  │
    │                                                    └─CHUNKED_AS─► Chunk_v2 (bge-m3 1024d)
    │
    ├─ build_cmd, test_cmd, lint_cmd, run_cmd, portforward_cmds
    ├─ runbook_md (LLM-synthesized)
    └─ conventions_md
```

`Symbol_v2.CALLS` links callers to callees (confidence-tagged: 1.0 same-file, 0.7 import-resolved, 0.4 fuzzy).

`File_v2 -[:IMPORTS]-> File_v2` carries the import graph.

## Stages

| Stage | What | Deps |
|---|---|---|
| 1 | RepoMix dump → markdown pack + sha256 | `repomix` (`npm i -g repomix`) |
| 2 | LLM repo summary → Repo node + RUNBOOK | qwen3.6 / OpenAI-compat |
| 3 | LLM service extract + operator override (`.aiforge/services.yaml`) | LLM |
| 4 | tree-sitter walk → File_v2 + Symbol_v2 + IMPORTS | tree-sitter-language-pack |
| 5 | tree-sitter call edges → CALLS / EXTENDS / IMPLEMENTS | (same) |
| 6 | LLM per-file summary + purpose_tags | LLM |
| 7 | bge-m3 chunk embeddings (1024d) | embed sidecar |
| 8 | DEPENDS_ON edges (service→service) | optional |
| 9 | learner write-back (post-ticket) | optional |

## Read API

```python
from aiforge_memory.query import bundle
from neo4j import GraphDatabase

drv = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j","password"))
ctx = bundle.query("fix payment processing in api", repo="my-repo", driver=drv)
print(ctx.render())
```

Or via the convenience helper that opens the driver for you:

```python
from aiforge_memory.api.read import context_bundle_for
md = context_bundle_for("fix payment", repo="my-repo")
```

## CLI

```
aiforge-memory doctor                    # check repomix / neo4j / llm
aiforge-memory ingest <repo> --path DIR  # full ingest (Stages 1-7)
aiforge-memory ingest <repo> --force     # bypass sha-cache
aiforge-memory stats <repo>              # repo node summary
aiforge-memory services <repo>           # services + file_count
```

## Per-layer test gates

Every layer has a README + golden test + make target:

```
make test-L1   # Repo node ingest
make test-L2   # Service extract + override
make test-L3   # File summary
make test-L4   # Tree-sitter symbols + call edges
make test-L5   # Chunk embeddings
make test-L6   # Translator (NL→entities)
make test-L7   # Bundle (full query path)
```

Each gate runs against a small fixture repo and (where needed) hits a real Neo4j. Mocked LLM/embed responses make L1–L5 deterministic; L6–L7 mock the LLM and use a recorded vector.

Run all in one shot:

```
make test
```

## Required infrastructure

- **Neo4j 5.x** (Community OK; AiForgeMemory uses composite uniqueness instead of NODE KEY).
  `bolt://127.0.0.1:7687` is the default.
- **`repomix` CLI** on PATH: `npm i -g repomix`
- **OpenAI-compat LLM endpoint** — typically LM Studio / Ollama / mlx-lm.
  Default URL: `http://127.0.0.1:1235/v1` (override with `AIFORGE_CODEMEM_LM_URL`).
- **bge-m3 embed sidecar** with `POST /embed {text}` returning `{embedding: [1024]}`.
  Default URL: `http://127.0.0.1:8764` (override with `AIFORGE_EMBED_URL`).

## Storage

State (idempotency) lives in `~/.aiforge/codemem.state.db` (sqlite). Wipe with `aiforge-memory reset <repo>` (planned).

Repo / Service / File_v2 / Symbol_v2 / Chunk_v2 nodes carry `schema_version: 'codemem-v1'` for targeted rollback:

```cypher
MATCH (n) WHERE n.schema_version='codemem-v1' DETACH DELETE n
```

## Coexistence with other Neo4j tenants

If your Neo4j already hosts another tool's `:File` or `:Symbol` constraints, AiForgeMemory's labels are namespaced as `_v2` to avoid collisions. After full migration, drop the suffix in a single migration script.

## Status

Layers L1–L7 shipped + green. L8 (host UI rewire) is consumer-side. See `docs/spec.md` (port from upstream once published).

## License

MIT.
