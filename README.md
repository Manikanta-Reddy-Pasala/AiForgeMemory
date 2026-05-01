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
   └─ translator
        ├─ /embed → vector top-K   (semantic recall)
        ├─ Lucene fulltext on Symbol fqname/signature  (literal recall)
        └─ Qwen-Coder JSON-strict grounding (picks from candidate set only)
              │
              ▼
        Cypher traversal → ContextBundle
```

Dual-channel recall — fulltext catches literal-keyword queries even when L5 vector coverage is partial.

**Real PCB query latency:** 5–8 s end-to-end (translator + bundle).

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

## Real PCB graph (PosClientBackend, 5040 Java files)

| | Count |
|---|---|
| Files | 5,040 |
| Symbols | 25,920 (5,380 classes + 20,540 methods) |
| IMPORTS | 10,770 |
| CALLS | 44,940 — 49% high-confidence (1.0 same-file + 0.7 import-aware) |
| Services | 6 (operator yaml) |
| File summaries | 4,705 |
| Chunks | 691 (partial; full coverage = ~3 hr embed time) |

Sample query: **"which api used to save sales data"** → `sales/SaleService::save` as top symbol in 8 s.

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
| RepoMix CLI | `npm i -g repomix` | Stage 1 |

Run `make doctor` to verify.

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
