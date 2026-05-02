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
Repo ─OWNS_SERVICE─► Service ─CONTAINS_FILE─► File_v2 ─DEFINES─► Symbol_v2
  │                                              │                  │
  │                                              └─CHUNKED_AS─► Chunk_v2 (1024d bge-m3)
  │
  ├─RECORDS─► Decision_v2 / Observation_v2 / Note_v2 / Doc_v2
  │                  │
  │                  └─MENTIONS─► File_v2 / Symbol_v2
  │
  └─CALLS_REPO─► Repo   (via=http | nats | shared_collection)
```

Per-Repo: `build_cmd`, `test_cmd`, `run_cmd`, `runbook_md`.
Per-Symbol: `CALLS`, `EXTENDS`, `IMPLEMENTS` edges (confidence-tagged).
Per-File: `IMPORTS` edges, LLM `summary`, `purpose_tags`.
Per-Memory: `MENTIONS` anchors decisions/observations to actual symbols/files; vector index on `Observation_v2.embed_vec` for semantic recall.
Per-RepoPair: `CALLS_REPO` links repos that share an HTTP route, NATS subject, or MongoDB collection.

### Emitted property contract

| Label | Properties |
|---|---|
| `Repo` | `name`, `path`, `lang_primary`, `build_cmd`, `test_cmd`, `lint_cmd`, `run_cmd`, `portforward_cmds`, `conventions_md`, `runbook_md`, `last_pack_sha`, `head_sha`, `branch`, `default_branch`, `remote_url`, `dirty`, `last_indexed_at`, `schema_version` |
| `Service` | `repo`, `name`, `description`, `role`, `tech_stack`, `port`, `source`, `schema_version` |
| `File_v2` | `repo`, `path`, `hash`, `lang`, `lines`, `parse_error`, `indexed_at`, `summary`, `purpose_tags`, `skipped_reason`, `schema_version` |
| `Symbol_v2` | `repo`, `fqname`, `kind`, `file_path`, `signature`, `doc_first_line`, `line_start`, `line_end`, `visibility` (public/private/protected/package), `modifiers` (static/final/abstract/...), `return_type`, `params_json` (JSON-encoded `[{name,type}]`), `deprecated`, `schema_version` |
| `Chunk_v2` | `id`, `repo`, `file_path`, `text`, `embed_vec`, `token_count`, `line_start`, `line_end`, `schema_version` |
| `Decision_v2` | `id`, `repo`, `title`, `body`, `rationale`, `status` (active/superseded/rejected), `author`, `session_id`, `tags`, `created_at`, `updated_at`, `schema_version` |
| `Observation_v2` | `id`, `repo`, `kind` (note/bug/learning/gotcha/feedback), `text`, `author`, `session_id`, `tags`, `embed_vec`, `embed_model`, `created_at`, `updated_at`, `schema_version` |
| `Note_v2` | `id`, `repo`, `title`, `body`, `author`, `tags`, `created_at`, `updated_at`, `schema_version` |
| `Doc_v2` | `id`, `repo`, `url`, `title`, `body`, `source_kind` (web/readme/runbook/api-spec), `fetched_at`, `created_at`, `schema_version` |
| `CALLS_REPO` (edge) | `via`, `evidence`, `confidence`, `created_at`, `updated_at`, `schema_version` |

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

CLI surface, full:

```
aiforge-memory ingest <repo> [--path DIR] [--force] [--delta] [--lsp]
aiforge-memory stats <repo>
aiforge-memory services <repo>

# Memory layer
aiforge-memory remember <repo> --type {decision|observation|note|doc} --text "..." [...]
aiforge-memory recall   <repo> --query "..." [--k N]
aiforge-memory forget   <repo> --type ... --id ID
aiforge-memory list-memory <repo> [--type ...]

# Cross-repo
aiforge-memory link --repos r1,r2,r3 [--min-confidence 0.0]
aiforge-memory link-list [--repo R]

# Eval
aiforge-memory eval <repo> --probes path.yaml [--table] [--fail-under 0.8]

# Hooks + scheduler
aiforge-memory install-hook <repo> [--path DIR]   # post-commit + post-merge
aiforge-memory schedule add <repo> --path DIR --interval 600 [--no-pull]
aiforge-memory schedule {list|remove|run|daemon|stop|status}

aiforge-memory doctor
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
| `make test-L8` | Memory layer (Decision/Observation/Note/Doc) | sqlite + Neo4j |
| `make test-L9` | Cross-repo `CALLS_REPO` extraction | pure-python |
| `make test-L10` | Delta ingest (git diff + merkle) | pure-python |
| `make test-L11` | Eval harness aggregation | pure-python |
| `make test-L12` | Scheduler config + lock + git poll-decide | pure-python |
| `make test-L13` | Symbol enrichment (visibility/return_type/params) | tree-sitter fixtures |
| `make test-L14` | LSP wire codec + adapter + resolver | pure-python |
| `make test-L15` | git_meta against ephemeral git repo | git CLI |
| `make test-unit` | all no-infra gates | fast |
| `make test`    | all (~11 s) | 148 + 34 skipped (skips need live Neo4j) |

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

## Cross-repo (`CALLS_REPO`)

`Repo -[CALLS_REPO]-> Repo` is now populated by the `aiforge-memory link` pass:

| Signal | Edge `via` | Confidence basis |
|---|---|---|
| HTTP client URI matches another repo's `@RequestMapping` / FastAPI route | `http` | `|overlap| / |emitter.routes|` |
| NATS publisher subject matches another repo's `subscribe` / `@JetStreamListener` | `nats` | `|overlap| / |emitter.subjects|` |
| Same MongoDB collection name (via `@Document` / `getCollection`) | `shared_collection` | `|overlap| / |union(collections)|` |

```bash
# After ingesting two or more repos:
aiforge-memory link --repos PosClientBackend,PosServerBackend,PosPythonBackend
aiforge-memory link-list --repo PosClientBackend
```

Edge properties: `via`, `evidence` (overlapping tokens, capped at 10), `confidence` (0..1), `created_at`, `updated_at`. Idempotent — re-runs update evidence + confidence in place. Bundle queries hydrate a `## Related repos` section automatically when the queried repo participates in any edge.

---

## Memory layer

Code memory and *conversation* memory side-by-side in the same graph. Four memory node types, all keyed on a generated id and linked via `Repo-[:RECORDS]->Memory`. Refs become `Memory-[:MENTIONS]->File_v2|Symbol_v2`.

| Label | Purpose | Has embed? |
|---|---|---|
| `Decision_v2` | Durable architectural / process choices ("we picked NATS over Kafka because…") | no |
| `Observation_v2` | Notes, bugs, learnings, gotchas, feedback. Vector-recallable. | yes (bge-m3 1024d) |
| `Note_v2` | Free-form memos / ADRs / how-tos | no |
| `Doc_v2` | Web docs / external API specs ingested into the graph | no |

```bash
# Record a decision; refs become MENTIONS edges
aiforge-memory remember PosClientBackend \
  --type decision \
  --title "NATS over Kafka" \
  --text  "ADR-001: chose NATS JetStream for sync" \
  --why   "Lower ops overhead, sub-cluster ack semantics fit our offline mode." \
  --refs  "src/main/java/com/pos/backend/dataSync/PosServerBackendService.java" \
  --tags  "arch,messaging" \
  --author "manik"

# Observation with auto-embed (falls back gracefully when sidecar offline)
aiforge-memory remember PosClientBackend \
  --type observation --kind bug \
  --text "Race condition in MessageRetryService when batch=50 and durable consumer just rebalanced" \
  --refs "com.pos.backend.dataSync.MessageRetryService::pollAndRetry"

# Vector recall over Observation_v2
aiforge-memory recall PosClientBackend --query "race condition in retry"

# Mark a decision superseded
aiforge-memory remember PosClientBackend --type decision \
  --title "Switch to JetStream key-value bucket" --text "ADR-007" \
  --supersedes dec_a1b2c3d4e5f6

# List, forget
aiforge-memory list-memory PosClientBackend --type decision
aiforge-memory forget PosClientBackend --type observation --id obs_abc123
```

Bundle integration: when a query hits anchor files/symbols, decisions+observations linked via `MENTIONS` are surfaced under `## Decisions` and `## Observations` in the rendered ContextBundle. Repo-wide decisions (no `MENTIONS` edges) always surface for `active`/`superseded` rows.

---

## Delta ingest

Re-index only files changed since the last ingest. ~100× faster than a full re-run on a one-line edit.

```bash
aiforge-memory ingest PosClientBackend --delta
```

Detection strategy (in order):

1. **git diff** — if `.git` exists and a previous HEAD is recorded in `~/.aiforge/codemem.state.db`, diffs `prev_head..HEAD` (`--name-status -z`).
2. **merkle** — fallback when not a git checkout. Walks + hashes every source file, compares to `merkle_files`.
3. **cold** — first run with no prior state; auto-falls-through to a full ingest.

Deletions are honored: `File_v2` + descendant `Symbol_v2` + `Chunk_v2` are detached.

Optional: install a git `post-commit` hook so every commit triggers a delta ingest in the background.

```bash
aiforge-memory install-hook PosClientBackend
```

---

## Eval harness

Probes are simple yaml: NL query + expected files / symbols. Recall@K, MRR, latency are computed against them.

```bash
aiforge-memory eval PosClientBackend \
  --probes aiforge_memory/eval/probes.example.yaml --table

# Threshold gate (CI):
aiforge-memory eval PosClientBackend --probes probes.yaml --fail-under 0.8
```

Probe yaml example: see `aiforge_memory/eval/probes.example.yaml`. Each probe → one bundle.query call; metrics aggregate across all probes.

---

## Scheduler daemon

Periodic `git fetch` + `git pull --ff-only` + delta ingest, per repo. No external cron required for the polling itself; cron only restarts the daemon at boot and runs hourly maintenance.

```bash
# Add a repo to the schedule
aiforge-memory schedule add PosClientBackend \
  --path /Users/me/code/pcb \
  --interval 600                    # poll every 10 min
  # --no-pull                       # fetch only, don't pull
  # --skip-summaries --skip-chunks  # cheaper tick (no LLM, no embed)

aiforge-memory schedule list

# Run modes
aiforge-memory schedule run         # foreground (Ctrl-C to stop)
aiforge-memory schedule daemon      # POSIX double-fork; pidfile written
aiforge-memory schedule status      # JSON: pid + per-repo last_run/next_run
aiforge-memory schedule stop        # SIGTERM, waits up to 30s for actual exit, SIGKILL fallback
```

Safety + resilience (live-deployed across 42 OneShell repos on nuc):
- `git pull --ff-only` only — refuses divergent histories.
- Pull skipped when working tree dirty (tracked-file mods); ingest still runs and captures local state via merkle fallback.
- Per-repo lockfile (`~/.aiforge/lock.<repo>.pid`) prevents overlapping ticks; stale PIDs auto-reclaimed.
- **Per-tick wall timeout** (`timeout_seconds: 1800` per repo, configurable in `scheduler.yaml`) — one slow ingest can't block the loop.
- **Cold-start fallback** — if delta has no prior merkle state, auto-runs full ingest and writes file hashes so next tick is true delta.
- **Neo4j-down classification** — driver errors are tagged `neo4j_down`; the loop applies exponential backoff (15s → 300s) and re-opens the driver instead of spinning.
- **LSP per-repo opt-in** — set `use_lsp: true` in `scheduler.yaml` for any repo to layer LSP-confirmed CALLS on top of tree-sitter heuristic.
- SIGINT/SIGTERM → in-flight delta finishes, then exit.

`scheduler.yaml` shape (live config used in production):

```yaml
repos:
  - name: PosClientBackend
    path: /home/mani/codeRepo/PosClientBackend
    interval_seconds: 600
    pull: true
    skip_summaries: false
    skip_chunks: false
    use_lsp: false           # set true to enable LSP-confirmed CALLS
    timeout_seconds: 1800    # per-tick ceiling
  - name: oneshell-commons
    path: /home/mani/codeRepo/oneshell-commons
    interval_seconds: 600
    pull: true
```

Files:
- Config: `~/.aiforge/scheduler.yaml`
- Status: `~/.aiforge/scheduler.status.json`
- Log: `~/.aiforge/scheduler.log`
- Env: `~/.aiforge/env.sh` (sourced by hooks + cron)

`aiforge-memory install-hook <repo>` writes both `post-commit` AND `post-merge` hooks. The hook fires after local commits and after `git pull` succeeds.

**Symlink-safe install** — when an existing hook is a symlink to a *shared* dispatcher script (e.g. AIForgeCrew's `aiforge-reindex.sh`), the installer unlinks before writing instead of clobbering the shared target.

Hooks source `~/.aiforge/env.sh` so they have Neo4j/LM/embed URLs even when the user is not logged in. Output → `~/.aiforge/hook.log`.

---

## Health watchdog + ops

Sidecar liveness check + state snapshots + log rotation — all idempotent, all cron-friendly.

```bash
aiforge-memory health [--table]      # probe Neo4j + LM + embed + rerank
aiforge-memory ops backup --keep 14  # sqlite VACUUM INTO snapshot, rotate olds
aiforge-memory ops rotate-logs       # roll any log over 10MB; keep 5 generations
```

Recommended cron (deployed on nuc):

```cron
@reboot      sleep 30 && aiforge-memory schedule daemon       # restart after reboot
*/1 * * * *  aiforge-memory health                            # 1-min sidecar probe
17 3 * * *   aiforge-memory ops backup --keep 14              # nightly state backup
0 * * * *    aiforge-memory ops rotate-logs                   # hourly log rotation
```

The 1-min health check writes `~/.aiforge/health.json`. External monitors (alertmanager, ntfy, telegram bot) can tail that file and alert when `overall_ok: false` persists.

Backup target: `~/.aiforge/backups/codemem.state.<YYYYMMDD-HHMMSS>.db`. sqlite `VACUUM INTO` is atomic and safe under concurrent writes — daemon keeps running.

---

## LSP-confirmed CALLS (opt-in)

Tree-sitter heuristic produces CALLS edges with mixed confidence (~49% high-conf on PCB). Layer LSP-resolved edges on top — `confidence: 1.0` for any caller→callee pair the language server confirms via `textDocument/references`. Higher-confidence edge wins on overlap.

```bash
aiforge-memory ingest <repo> --lsp                  # full ingest with LSP
aiforge-memory ingest <repo> --delta --lsp          # delta with LSP
```

Per-language adapter discovery (auto-detected on PATH):

| Language | Server | Status |
|---|---|---|
| Python | `pyright-langserver` (preferred), `pylsp` fallback | supported |
| TypeScript / TSX | `typescript-language-server` | supported |
| JavaScript | `typescript-language-server` | supported |
| Java | `jdtls` (set `AIFORGE_JDTLS_CMD=<launch script>`) | experimental |

Adapter falls back gracefully when no server installed — tree-sitter heuristic remains source of truth. Add a new language by registering in `aiforge_memory/ingest/lsp/adapters.py::_ADAPTERS`.

---

## Required infrastructure

### External CLIs (host)

| Tool | Source | Purpose | Required |
|---|---|---|---|
| `git` | system | Repo metadata, delta diff, fetch/pull, hooks | yes |
| `repomix` | `npm i -g repomix` | Stage 1 — pack repo to single doc | yes for ingest |
| `python3` ≥3.11 | system | runtime | yes |
| `uv` | `~/.local/bin/uv` | venv + pip mgmt | recommended |
| `cron` | system | Scheduler @reboot, daily backup, hourly log rotate, 1-min health | yes for automation |
| `ssh` | system | Reverse tunnel from ms→nuc for LM Studio access | yes (deployment) |
| `pyright-langserver` | `npm i -g pyright` | LSP CALLS (Python) | optional (`--lsp`) |
| `typescript-language-server` | `npm i -g typescript-language-server` | LSP CALLS (TS/JS) | optional (`--lsp`) |
| `jdtls` | manual install + `AIFORGE_JDTLS_CMD` | LSP CALLS (Java) | optional + experimental |

### Sidecars (network services)

| Service | Default port | Purpose | Required |
|---|---|---|---|
| Neo4j 5 (Community, Docker) | `bolt://127.0.0.1:7687` | Graph store | **yes** |
| LM Studio (OpenAI-compat) | `:1234/v1` | Stages 2/3/6 + translator grounding | **yes** |
| bge-m3 embedding sidecar | `:8764` | 1024d chunk + observation + query embeddings | **yes for ingest+query** |
| Cross-encoder reranker | `:8765` | Translator step 5 (rerank top-30) | optional |

### Datastores

| Store | Path | Purpose | Backup |
|---|---|---|---|
| Neo4j | docker volume | All graph data | manual / docker volume snapshot |
| sqlite state | `~/.aiforge/codemem.state.db` | merkle hashes, git_state, service_overrides, query_cache | **`ops backup` daily cron** |

### Runtime files (per host)

| Path | Purpose |
|---|---|
| `~/.aiforge/env.sh` | Env vars sourced by hooks/cron/daemon |
| `~/.aiforge/scheduler.yaml` | Repo schedule (interval/pull/skip/use_lsp/timeout) |
| `~/.aiforge/scheduler.pid` | Daemon PID |
| `~/.aiforge/scheduler.status.json` | Per-repo last_run/next_run/status |
| `~/.aiforge/scheduler.log` | Rolling daemon log |
| `~/.aiforge/health.json` | Latest sidecar check snapshot |
| `~/.aiforge/health.cron.log` | 1-min health probe rolling log |
| `~/.aiforge/hook.log` | Hook-fired delta runs |
| `~/.aiforge/logs/reindex-*.log` | Per-repo reindex (graphify + delta) |
| `~/.aiforge/backups/codemem.state.*.db` | Daily DB snapshots (keep 14) |
| `~/.aiforge/lock.<repo>.pid` | Per-repo tick lock |
| `<repo>/.git/hooks/post-{commit,merge}` | Symlink or standalone hook |

Run `make doctor` (build/repomix/neo4j/llm) or `aiforge-memory health --table` (all sidecars + latency) to verify.

### Multi-host deployment (production recipe)

Live deployment used by 42 OneShell repos on a NUC + Mac Studio:

```
┌─ Mac Studio ─────────────┐         ┌─ NUC ─────────────────────────────┐
│  LM Studio (Qwen3-Coder) │ ───SSH──┤  scheduler daemon                  │
│  bound 127.0.0.1:1234    │  -R     │  hooks (post-commit/post-merge)    │
│                          │  1234   │  cron (@reboot, health, backup)    │
└──────────────────────────┘         │  Neo4j (docker)                    │
                                     │  bge-m3 + reranker sidecars        │
                                     │  ~/.aiforge/{env,sched,state}      │
                                     │  /home/mani/codeRepo/<42 repos>    │
                                     └────────────────────────────────────┘
```

Steps:
1. **Reverse SSH tunnel from ms → nuc** so nuc reaches LM Studio at `127.0.0.1:1234` (LM Studio binds localhost only).
2. **`uv pip install -e .[dev]`** on nuc.
3. **Generate `~/.aiforge/scheduler.yaml`** (one entry per repo).
4. **Write `~/.aiforge/env.sh`** with Neo4j + LM + embed + rerank URLs.
5. **`aiforge-memory install-hook <repo>`** for each repo (idempotent, symlink-safe).
6. **`aiforge-memory schedule daemon`** — POSIX double-fork.
7. **Install crons** (see Health watchdog + ops section).

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

**Hook fired but didn't index** — check `~/.aiforge/hook.log`. Usually one of:
- env vars missing → confirm `~/.aiforge/env.sh` exists and contains `AIFORGE_NEO4J_URI` etc.
- Sidecar down → run `aiforge-memory health --table`.
- Hook is a symlink to a clobbered shared script → `ls -l <repo>/.git/hooks/post-commit` and verify the target.

**Daemon won't restart** (`scheduler already running`) — old PID file. The fixed `schedule stop` waits up to 30s for actual exit and SIGKILLs as fallback. If still stuck:
```bash
pkill -9 -f "aiforge-memory schedule" && rm ~/.aiforge/scheduler.pid
aiforge-memory schedule daemon
```

**One slow ingest blocks everything** — the per-tick `timeout_seconds` (default 1800) bounds each repo's tick. Reduce via `scheduler.yaml` for known slow repos.

**LM Studio down on remote host** — health probe alerts within 60s. Daemon retries with exponential backoff (15s → 300s) and re-opens the driver each time. No manual restart needed once LM is back.

**State DB lost / corrupted** — restore from `~/.aiforge/backups/codemem.state.<latest>.db`:
```bash
cp ~/.aiforge/backups/codemem.state.20260502-031701.db ~/.aiforge/codemem.state.db
```
Worst case: delete `codemem.state.db` and let next tick re-pack from scratch (cold-start fallback).

**PCB-style 9k-symbol graph eats Neo4j memory** — bump heap in docker compose:
```yaml
environment:
  NEO4J_dbms_memory_heap_max__size: "4G"
  NEO4J_dbms_memory_pagecache_size: "2G"
```

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
