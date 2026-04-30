# AiForgeMemory

> Code intelligence backend for local-LLM dev tools.
> One read API. One graph model. One ingest pipeline.

When you ask "fix payment in the API service," AiForgeMemory hands the right files, methods, and runbook to your agent — without a 200-line aggregator stitched together in your prompt.

---

## What it gives you

```
       Plain-English query
              │
              ▼
   ┌────────────────────┐
   │  AiForgeMemory     │
   │  • embed match     │
   │  • LLM grounding   │
   │  • graph traversal │
   └─────────┬──────────┘
             │
             ▼
   ContextBundle ──►  agent prompt
   (services, files, symbols,
    runbook, call neighbours)
```

One function call. One Markdown block back. Drop into any agent prompt.

---

## How code is stored

```
   Repo ─OWNS_SERVICE─►  Service ─CONTAINS_FILE─►  File_v2 ─DEFINES─►  Symbol_v2
    │                                                  │                  │
    │                                                  └─CHUNKED_AS─► Chunk_v2
    │                                                                 (1024-d vector)
    ▼
  build_cmd, test_cmd, lint_cmd, run_cmd
  portforward_cmds
  runbook_md      ◄── how to clone / build / test / run / debug
  conventions_md
```

Four levels = four query shapes:

| Question                          | Where it answers from |
|-----------------------------------|-----------------------|
| "How do I run this repo?"         | `Repo.runbook_md`     |
| "Which services consume X?"       | `Service` + `DEPENDS_ON` |
| "What does this file do?"         | `File_v2.summary` (LLM, ≤200 tok) |
| "Who calls this method?"          | `Symbol_v2` + `CALLS` |
| "Show me code about X"            | `Chunk_v2` vector search |

---

## How code gets in

```
   git repo
      │
      ▼
   ┌─────────────────────────────────────────────┐
   │  Stage 1   Repomix          → markdown pack │
   │  Stage 2   LLM repo summary → Repo + RUNBOOK│
   │  Stage 3   LLM service      → Services      │
   │  Stage 4   tree-sitter      → Files+Symbols │
   │  Stage 5   tree-sitter      → CALLS edges   │
   │  Stage 6   LLM file summary → File summary  │
   │  Stage 7   bge-m3 embed     → Chunk vectors │
   └─────────────────────────────────────────────┘
                       │
                       ▼
                  Neo4j graph
```

Idempotent — re-runs that find unchanged files are no-ops.

---

## Quick start

```bash
# 1. Install
make install
make doctor          # check repomix / Neo4j / LLM / embed sidecar

# 2. Ingest a repo
aiforge-memory ingest my-app --path /path/to/my-app

# 3. Query
python -c "
from aiforge_memory.api.read import context_bundle_for
print(context_bundle_for('fix payment processing', repo='my-app'))
"
```

---

## Test every layer

Each layer has its own gate test + README + Make target.

```
make test-L1   # Repo node ingest          (RepoMix + LLM)
make test-L2   # Services + override       (LLM)
make test-L3   # File summaries            (LLM)
make test-L4   # Symbols + call edges      (tree-sitter)
make test-L5   # Chunk embeddings          (bge-m3)
make test-L6   # Translator                (NL → entities)
make test-L7   # Bundle                    (full pipeline)

make test     # all (~1 min)
```

Status today: **81 / 81 green.**

---

## Real-service smoke

PosClientBackend (5,040 Java files):

```
Stage 4 (walk):           17.7 s
Stage 4 (write symbols):  185 s   →  26,220 symbols
Stage 5 (resolve calls):  20.4 s
Stage 5 (write CALLS):    192 s   →  73,151 edges (44,862 unique after MERGE)
                          ────
                          ~7 min, no LLM needed
```

LLM stages (2, 3, 6) run on a separate cadence.

---

## Architecture in one picture

```
┌─────────────────  AiForgeMemory  ─────────────────┐
│                                                   │
│  ingest/                                          │
│   ├── pack_repo.py          Stage 1               │
│   ├── repo_summary.py       Stage 2  (LLM)        │
│   ├── service_extract.py    Stage 3  (LLM)        │
│   ├── treesitter_walk.py    Stage 4               │
│   ├── edges.py              Stage 5               │
│   ├── file_summary.py       Stage 6  (LLM)        │
│   ├── embed.py              Stage 7               │
│   └── flow.py               orchestrator          │
│                                                   │
│  store/                  Cypher writers           │
│   ├── schema.py          constraints + indices    │
│   └── *_writer.py        per-node-type writes     │
│                                                   │
│  query/                                           │
│   ├── fastpath.py        regex bypass             │
│   ├── translator.py      embed + LLM grounding    │
│   └── bundle.py          ContextBundle builder    │
│                                                   │
│  api/                                             │
│   ├── cli.py             aiforge-memory *         │
│   └── read.py            context_bundle_for(...)  │
│                                                   │
└───────────────────────────────────────────────────┘
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
     Neo4j 5            sqlite (idempotency
   (knowledge graph)     hashes, query cache)
```

---

## Required infrastructure

| Service              | Default URL              | What it's for           |
|----------------------|--------------------------|-------------------------|
| Neo4j 5 (Community)  | `bolt://127.0.0.1:7687`  | Knowledge graph         |
| LM Studio / Ollama   | `http://127.0.0.1:1235`  | Stages 2, 3, 6 (LLM)    |
| bge-m3 sidecar       | `http://127.0.0.1:8764`  | Stage 7 (embeddings)    |
| RepoMix CLI          | `npm i -g repomix`       | Stage 1 (pack)          |

Run `make doctor` to verify.

Override any default with the matching `AIFORGE_*` env var (see `.env.example`-style block in source).

---

## CLI

```
aiforge-memory doctor                    # health check
aiforge-memory ingest <repo> --path DIR  # full ingest
aiforge-memory ingest <repo> --force     # bypass sha cache
aiforge-memory stats <repo>              # repo node summary
aiforge-memory services <repo>           # services + file_count
```

---

## Read API

```python
from aiforge_memory.api.read import context_bundle_for

md = context_bundle_for(
    "fix payment processing in the api",
    repo="my-app",
    role="doer",
    token_budget=4000,
)
# md is a Markdown block ready for any LLM prompt
```

For structured access:

```python
from aiforge_memory.query import bundle
from neo4j import GraphDatabase

drv = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j","password"))
b = bundle.query("fix payment", repo="my-app", driver=drv)
b.services    # → [{name, role, port, ...}]
b.files       # → [{path, summary, purpose_tags}]
b.symbols     # → [{fqname, kind, signature}]
b.callers     # → [{fqname, target}]
b.callees     # → [{fqname, source}]
b.runbook_md  # → str
```

---

## Coexistence

If your Neo4j already hosts another tool's `:File` or `:Symbol`, AiForgeMemory uses `_v2`-suffixed labels (`File_v2`, `Symbol_v2`, `Chunk_v2`) so there's zero collision. Drop the suffix in a single migration when you're ready.

Every node carries `schema_version: 'codemem-v1'` for targeted rollback:

```cypher
MATCH (n) WHERE n.schema_version = 'codemem-v1' DETACH DELETE n
```

---

## What's not here yet

- **DEPENDS_ON edges** between services (Stage 8) — code stub, needs wiring
- **Learner write-back** (Stage 9) — appending ticket lessons to `File.summary`
- **Cross-repo edges** (`Repo.CALLS_REPO`) — separate spec
- **Maven/Gradle path resolution** for Java IMPORTS — v1 falls back to fuzzy CALLS

---

## Origin

Extracted from [AIForgeCrew](https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew). Design spec lives there at `docs/superpowers/specs/2026-04-30-unified-code-memory-design.md`.

---

## License

MIT.
