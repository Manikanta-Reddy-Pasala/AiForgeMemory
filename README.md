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

## Real-service smoke (PosClientBackend, 5,040 Java files)

| Stage | Wall | Output |
|---|---|---|
| 1 Pack (RepoMix) | 4 s | 27.2 M chars |
| 2 Repo summary (Qwen-Coder) | 55 s | runbook + build/test/run cmds |
| 3 Services (yaml override + glob) | <1 s | 6 services, 49 file edges |
| 4 Symbols (tree-sitter) | ~3 min | 25,920 symbols (5,380 classes + 20,540 methods) |
| 5 CALLS (Maven path resolver) | ~3 min | 73,229 edges → 44,940 unique. Confidence: 1.0 → 19% / 0.7 → 30% / 0.4 → 51%. **49% high-conf** thanks to Maven `src/main/java/` path-strip |
| 5 IMPORTS | (same) | 10,770 edges |
| 6 File summaries (LLM, 6w concurrent) | 69 min | 4,705 / 4,705 files summarized |
| 7 Chunk embeddings (8w + /embed_batch) | partial | 691 chunks / 375 files (single-instance bge-m3 is the cap) |

**Real query end-to-end:** 8 s — `"which api used to save sales data"` → returns `sales/SaleService.save` as top symbol on the real PCB graph.

## Translator recall path (two channels)

The translator hydrates LLM-grounding candidates from **both** vector and fulltext, so partial L5 coverage doesn't kill recall:

```
NL query
   │
   ├──► /embed (bge-m3) → vector top-K against codemem_chunk_embed
   │      (semantic match — needs Stage 7 coverage)
   │
   └──► Lucene OR-tokens → codemem_symbol_signature_ft
          (literal-name match — works on every Symbol regardless of L5)
              │
              ▼
       merged candidate set + service catalog
              │
              ▼
       Qwen-Coder JSON-strict grounding (picks from candidates only)
```

Fulltext is the safety-net. Without it, "save sales" falls back to whichever ~7% of files happened to be embedded; with it, every `Sale*::save` symbol is reachable.

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

## Configuration — single yaml per repo

Create `.aiforge/codemem.yaml` at the repo root. All fields optional; env vars are fallback.

```yaml
repo:
  name: my-app                                # logical name (Repo.name in Neo4j)
  path: /home/me/codeRepo/my-app

knowledge:                                    # additional docs to surface in bundles
  readmes:
    - README.md
    - docs/ARCHITECTURE.md
    - docs/RUNBOOK.md
  conventions:
    - .aiforge/CONVENTIONS.md
  exclude:
    - target/**
    - vendor/**

services_yaml: .aiforge/services.yaml         # operator catalog (skips slow LLM Stage 3)

ingest:
  skip_services: false
  skip_symbols:  false
  skip_summaries: false                       # Stage 6 (set true if rate-limited)
  skip_chunks:   false                        # Stage 7
  file_summary_max_bytes: 32768
  embed_max_bytes:        65536

llm:
  url: http://127.0.0.1:1234/v1               # LM Studio / Ollama / etc.
  model: /path/to/Qwen3-Coder-Next-MLX-4bit
  api_key: lm-studio
  repo_summary_max_tokens: 8000

embed:
  url: http://127.0.0.1:8764                  # bge-m3 sidecar

neo4j:
  uri: bolt://127.0.0.1:7687
  user: neo4j
  password: password
```

Auto-loaded by `aiforge-memory ingest` and the `RepoConfig.load()` helper. Env vars (e.g. `AIFORGE_NEO4J_URI`) win over yaml when both set — useful for CI overrides.

---

## How to use it (after install)

```bash
# 1. install + verify infra
make install
make doctor

# 2. drop a config + (optional) services.yaml in your repo
mkdir -p ~/codeRepo/my-app/.aiforge
cat > ~/codeRepo/my-app/.aiforge/codemem.yaml <<EOF
repo:
  name: my-app
EOF

# 3. ingest
aiforge-memory ingest my-app --path ~/codeRepo/my-app

# 4. verify
aiforge-memory stats my-app
aiforge-memory services my-app
```

---

## How to query

### From Python

```python
from aiforge_memory.api.read import context_bundle_for
md = context_bundle_for("which api saves sales data", repo="my-app")
print(md)
```

### From CLI (planned, currently via Python REPL)

```python
from aiforge_memory.query import bundle
from neo4j import GraphDatabase

drv = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j","password"))
b = bundle.query("fix payment processing", repo="my-app", driver=drv)
print(b.render())
```

### Query types it answers

| Question shape | What lights up |
|---|---|
| `Class.method` literal | fastpath → exact symbol lookup |
| `TICKET-123` | fastpath → ticket history join |
| `path/to/file.ext` | fastpath → file lookup + neighbours |
| "which api saves X" | translator: vector + **fulltext** → LLM grounds → top symbols |
| "how do I run/test" | Repo runbook block |
| "which services consume Y" | Service catalog + DEPENDS_ON traversal |

---

## How to teach it more

The graph improves automatically as you ingest. To boost quality manually:

1. **Operator-curated services** (`.aiforge/services.yaml`)
   ```yaml
   services:
     - name: payment_api
       description: "Payment processing REST endpoints"
       role: api
       file_glob: src/main/java/com/co/payment/**/*.java
   ```
   `source: 'manual'` services stick on re-ingest; LLM-extracted ones can drop.

2. **Knowledge files** (`knowledge.readmes` / `conventions` in `codemem.yaml`)
   Each listed file gets prepended to the bundle's runbook section.

3. **Re-run Stage 6** after major refactors
   ```bash
   aiforge-memory ingest my-app --force          # re-summarize all changed files
   ```

4. **Custom queries during dev**
   ```python
   bundle.query("which methods publish to NATS subject business.push", repo="...")
   ```
   Refines the catalog as the LLM picks symbols you already cared about.

---

## Cross-repo links (planned, edge reserved)

The graph reserves `Repo -[CALLS_REPO]-> Repo` for cross-repo intelligence.

### Today (v0.1)

Each repo is its own connected component. `Service` nodes are scoped per-`Repo.name`. Same `Service` name in two repos = two distinct nodes.

### Roadmap

The cross-repo edge is populated by a separate post-ingest pass that scans:

1. **HTTP client calls** — when `RepoA` has `restTemplate.post("/v1/payments/...")` and `RepoB` exposes `@RequestMapping("/v1/payments")`, emit `(RepoA)-[:CALLS_REPO {via:'http', endpoint:'/v1/payments'}]->(RepoB)`.

2. **NATS subjects** — when `RepoA` publishes `business.push.request` and `RepoB` consumes it, emit `(RepoA)-[:CALLS_REPO {via:'nats', subject:'business.push.request'}]->(RepoB)`.

3. **Database collection sharing** — `RepoA` writes to MongoDB collection `X`, `RepoB` reads it → emit edge with `via:'shared_collection'`.

To run today (manual recipe; will become a CLI subcommand `aiforge-memory link`):

```python
# Pseudocode — see docs/cross-repo-link.md (TBD)
from neo4j import GraphDatabase
drv = GraphDatabase.driver(...)

# 1. find HTTP endpoints exposed by each Repo (Symbol.signature regex)
# 2. find HTTP client calls (call site signature regex)
# 3. match by URI substring → emit CALLS_REPO edge
```

This is a bonus stage (Plan 11 in the design spec). Until it ships, single-repo answers are still strong because:

- `Service.tech_stack` and `Service.description` carry enough hint that an LLM agent can pick the right service catalog when given multiple repos as candidates.
- Operators can hand-write `services.yaml` entries that reference external repos by name (e.g. `description: "consumes business.push.request from PosClientBackend"`).

When you land Plan 11, every existing graph automatically becomes a multi-repo knowledge graph — no re-ingest needed.

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
