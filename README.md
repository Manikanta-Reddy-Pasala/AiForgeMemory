# AiForgeMemory

Code intelligence backend for local-LLM dev tools. One read API. One graph. One ingest pipeline.

NL query → grounded answer with anchor files, symbols, runbook — in seconds.

```
"which api saves sales data?"
        │
        ▼  AiForgeMemory  ─►  ContextBundle markdown
                              ├─ services
                              ├─ files (with summaries)
                              ├─ symbols + call neighbours
                              └─ runbook (build/test/run)
                                       ▼
                                 agent prompt
```

---

## Graph model (Neo4j 5)

```
Repo  ─OWNS_SERVICE─►  Service  ─CONTAINS_FILE─►  File_v2 ─DEFINES─►  Symbol_v2
                                                     │                   │
                                                     └─CHUNKED_AS─► Chunk_v2 (1024d bge-m3)
```

Per-Repo: `build_cmd`, `test_cmd`, `run_cmd`, `runbook_md`.
Per-Symbol: `CALLS`, `EXTENDS`, `IMPLEMENTS` edges (confidence-tagged).
Per-File: `IMPORTS` edges, LLM `summary`, `purpose_tags`.

---

## Ingest pipeline

| Stage | What | Wall (PCB 5040 files) |
|---|---|---|
| 1 | Repomix pack | 4 s |
| 2 | LLM repo summary → `Repo` | 55 s |
| 3 | LLM service catalog (operator yaml override + glob) | <1 s w/ override |
| 4 | tree-sitter walk → `File_v2` + `Symbol_v2` + `IMPORTS` | 3 min |
| 5 | call edges + Maven/Gradle path resolver → `CALLS` | 3 min |
| 6 | per-file LLM summary (concurrent workers) | 69 min @ 6 workers |
| 7 | bge-m3 chunks via `/embed_batch` | bottlenecked by sidecar |

Idempotent — re-runs skip unchanged files via merkle hash.

---

## Query path

```
NL text
   ├─ fastpath?  (Class.method | TICKET-123 | path/to/file.ext)
   │     └─ direct Neo4j lookup
   │
   └─ translator (6-stage hybrid retrieval)
        1. query expansion       (synonyms: auth↔jwt, crud↔controller …)
        2. vector top-K          (bge-m3 → Cypher chunk index)
        3. fulltext on symbols   (CamelCase split + Lucene-escaped)
        4. RRF fusion + path-prior (Controller / Test / Dto cues)
        5. cross-encoder rerank  (sidecar :8765, top-30 reordered)
        6. 1-hop graph expansion (IMPORTS in/out for top-5 files)
              │
              ▼
        Qwen-Coder JSON-strict grounding (picks from candidate set only)
              │
              ▼
        Cypher traversal → ContextBundle
```

Hybrid recall — RRF-fused embed + Lucene + path-prior survives bad queries; rerank tightens the top; 1-hop catches the rest.

**Real PCB query latency:** 9–15 s end-to-end (translator + bundle, rerank+1-hop on).
**Real PCB recall:** 10/10 NL probes return ≥1 semantically-relevant file (PosClientBackend, post worktree purge).

Tunables (env, default in parens):

| Var | Default | What |
|---|---|---|
| `AIFORGE_TRANSLATOR_RERANK` | `1` | enable cross-encoder rerank |
| `AIFORGE_TRANSLATOR_RERANK_TOPN` | `30` | rerank window |
| `AIFORGE_TRANSLATOR_RRF_K` | `60` | RRF fusion constant |
| `AIFORGE_RERANK_URL` | `http://127.0.0.1:8765` | rerank sidecar |

---

## Quickstart

```bash
make install
make doctor                   # check repomix + Neo4j + LLM + embed sidecar
```

Drop a config in your repo:

```yaml
# /path/to/my-app/.aiforge/codemem.yaml
repo:
  name: my-app
llm:
  url:   http://127.0.0.1:1234/v1
  model: /path/to/Qwen3-Coder-Next-MLX-4bit
neo4j:
  uri:   bolt://127.0.0.1:7687
```

Ingest + query:

```bash
aiforge-memory ingest my-app --path /path/to/my-app
aiforge-memory stats my-app
aiforge-memory services my-app
```

```python
from aiforge_memory.api.read import context_bundle_for
print(context_bundle_for("which api saves sales data", repo="my-app"))
```

---

## Configuration

All fields optional. Env vars (`AIFORGE_*`) win over yaml.

```yaml
repo:        { name, path }
knowledge:   { readmes: [...], conventions: [...], exclude: [glob...] }
services_yaml: .aiforge/services.yaml
ingest:
  skip_services / skip_symbols / skip_summaries / skip_chunks: bool
  file_summary_max_bytes: 32768
  embed_max_bytes:        65536
llm:
  url / model / api_key / repo_summary_max_tokens
embed:
  url
neo4j:
  uri / user / password
```

---

## Operator services.yaml

Bypass slow Stage 3 LLM with a hand-written catalog (uses globs):

```yaml
services:
  - name: data_sync
    description: "Push/pull sync to PosServerBackend over NATS"
    role: consumer
    file_glob: src/main/java/com/pos/backend/dataSync/**/*.java
  - name: api
    role: api
    port: 8090
    file_glob: src/main/java/com/pos/backend/feature/**/*.java
```

`source: 'manual'` — survives re-ingest.

---

## Test gates

| | | |
|---|---|---|
| `make test-L1` | Repo node | RepoMix + LLM |
| `make test-L2` | Services + override | LLM + glob |
| `make test-L3` | File summaries | LLM |
| `make test-L4` | Symbols + CALLS | tree-sitter |
| `make test-L5` | Chunks | bge-m3 |
| `make test-L6` | Translator | NL → entities |
| `make test-L7` | Bundle | full pipeline |
| `make test`    | all (~1 min) | 87 / 87 green |

---

## Real PCB graph (PosClientBackend, 1007 canonical Java files)

| | Count |
|---|---|
| Files | 1,007 (post worktree purge — 4,033 `.aiforge-worktrees/**` dupes removed) |
| Symbols | 5,174 |
| Chunks | 2,693 |
| IMPORTS | populated by tree-sitter walk |
| CALLS | 49% high-confidence (1.0 same-file + 0.7 import-aware) |
| Services | 6 (operator yaml) |

Sample probes (real, 10/10 PASS):

| Query | Top file |
|---|---|
| "Add ledgerCategory CRUD APIs" | `feature/ledger/LedgerMappingController.java` |
| "where is the data sync push flow" | `dataSync/PosServerBackendService.java` |
| "validation rules for sales transaction" | `saga/workflows/SalesWorkflow.java::validateSales` |
| "where is JWT auth handled" | `feature/login/LogInValidationServiceImpl.java::getUserToken` |
| "BusinessProductsController endpoints" | exact file + 8 endpoint methods |

> **Note**: ingest now filters `.aiforge-worktrees/**` paths so prior agent
> worktree dirs no longer pollute the index. Existing graphs can be
> cleaned with the snippet in the troubleshooting section below.

---

## Cross-repo (planned, edge reserved)

`Repo -[CALLS_REPO]-> Repo` is reserved in the schema. A separate `aiforge-memory link` pass will populate it:

| Signal | Edge `via` |
|---|---|
| HTTP client URI matches another repo's `@RequestMapping` | `http` |
| NATS publisher subject matches another repo's consumer | `nats` |
| Same MongoDB collection name in both repos | `shared_collection` |

Runs as post-ingest pass; no graph rebuild needed.

---

## Required infrastructure

| Service | Default | What it's for |
|---|---|---|
| Neo4j 5 (Community) | `bolt://127.0.0.1:7687` | Graph |
| LM Studio / Ollama | `http://127.0.0.1:1234/v1` | Stages 2/3/6 + translator |
| bge-m3 sidecar | `http://127.0.0.1:8764` | Stage 7 + query embedding |
| Cross-encoder reranker | `http://127.0.0.1:8765` | Translator step 5 (optional) |
| RepoMix CLI | `npm i -g repomix` | Stage 1 |

Run `make doctor` to verify.

---

## Troubleshooting

**Worktree pollution** — agent runtimes (e.g. AIForgeCrew) create
`.aiforge-worktrees/<ticket>/...` dirs. These resemble source files and
get indexed unless filtered. Symptom: query top-K dominated by
`.aiforge-worktrees/...` paths. Fix:

```cypher
MATCH (f:File_v2)-[:CHUNKED_AS]->(c:Chunk_v2)
WHERE f.path STARTS WITH ".aiforge-worktrees/"
DETACH DELETE c;
MATCH (f:File_v2)-[:DEFINES]->(s:Symbol_v2)
WHERE f.path STARTS WITH ".aiforge-worktrees/"
DETACH DELETE s;
MATCH (f:File_v2) WHERE f.path STARTS WITH ".aiforge-worktrees/"
DETACH DELETE f;
```

**Translator returns nothing** — check the translator LLM URL/model
(`AIFORGE_INTENT_LM_URL`, `AIFORGE_CODEMEM_LM_MODEL`); they default to
`:1235` which is *not* the same as the agent LLM `:1234`.

**Rerank slow / wrong** — disable with `AIFORGE_TRANSLATOR_RERANK=0`
(falls back to RRF order). Or change top-N via
`AIFORGE_TRANSLATOR_RERANK_TOPN`.

---

## Coexistence

Uses `_v2`-suffixed labels (`File_v2`, `Symbol_v2`, `Chunk_v2`) to avoid colliding with other Neo4j tenants. Drop the suffix in a single migration once you're sole tenant.

Every node carries `schema_version: 'codemem-v1'` for targeted rollback:

```cypher
MATCH (n) WHERE n.schema_version = 'codemem-v1' DETACH DELETE n
```

---

## Origin

Extracted from [AIForgeCrew](https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew). Design spec: `docs/superpowers/specs/2026-04-30-unified-code-memory-design.md` upstream.

MIT.
