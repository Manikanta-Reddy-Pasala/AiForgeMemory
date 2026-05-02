# AiForgeMemory

Code intelligence backend for local-LLM dev tools. NL query → grounded answer (anchor files, symbols, runbook, decisions, cross-repo edges) in seconds.

```
"which api saves sales data?" → ContextBundle → agent prompt
```

---

## Graph (Neo4j 5)

```
Repo ─OWNS_SERVICE─► Service ─CONTAINS_FILE─► File_v2 ─DEFINES─► Symbol_v2
  │                                              │                  │
  │                                              └─CHUNKED_AS─► Chunk_v2 (1024d bge-m3)
  │
  ├─RECORDS─► Decision_v2 / Observation_v2 / Note_v2 / Doc_v2
  │                  └─MENTIONS─► File_v2 / Symbol_v2
  │
  └─CALLS_REPO─► Repo   (via=http | nats | shared_collection)
```

`Symbol_v2`: `CALLS` (confidence 0.4 fuzzy / 0.7 import / 1.0 same-file or LSP), `EXTENDS`, `IMPLEMENTS`.
`File_v2`: `IMPORTS`, LLM `summary`, `purpose_tags`.
All nodes: `schema_version: 'codemem-v1'` for targeted rollback.

### Emitted properties

| Label | Properties |
|---|---|
| `Repo` | name, path, lang_primary, build/test/lint/run_cmd, portforward_cmds, conventions_md, runbook_md, last_pack_sha, head_sha, branch, default_branch, remote_url, dirty, last_indexed_at |
| `Service` | repo, name, description, role, tech_stack, port, source |
| `File_v2` | repo, path, hash, lang, lines, parse_error, indexed_at, summary, purpose_tags, skipped_reason |
| `Symbol_v2` | repo, fqname, kind, file_path, signature, doc_first_line, line_start, line_end, visibility, modifiers, return_type, params_json, deprecated |
| `Chunk_v2` | id, repo, file_path, text, embed_vec, token_count, line_start, line_end |
| `Decision_v2` | id, repo, title, body, rationale, status (active/superseded/rejected), author, session_id, tags, created_at, updated_at |
| `Observation_v2` | id, repo, kind (note/bug/learning/gotcha/feedback), text, author, session_id, tags, embed_vec, embed_model, created_at |
| `Note_v2` / `Doc_v2` | id, repo, title, body, [url, source_kind for Doc], created_at |
| `CALLS_REPO` (edge) | via, evidence (capped 10), confidence, created_at, updated_at |

---

## Ingest (7 stages, idempotent on pack_sha)

| # | Stage | Output | Wall (PCB ~5k files) |
|---|---|---|---|
| 1 | repomix pack | text + sha | 4s |
| 2 | LLM repo summary | `Repo` + git_meta | 55s |
| 3 | LLM service catalog (or operator yaml) | `Service` + edges | <1s w/ override |
| 4 | tree-sitter walk | `File_v2` + `Symbol_v2` (+ visibility/return_type/params) + `IMPORTS` | 3min |
| 5 | call edges (path-resolved) + optional LSP | `CALLS` (1.0 / 0.7 / 0.4) | 3min |
| 6 | per-file LLM summary (concurrent) | `File_v2.summary` + `purpose_tags` | 69min @ 6 workers |
| 7 | bge-m3 chunks via `/embed_batch` | `Chunk_v2` + vector index | bottlenecked by sidecar |

Re-runs skip unchanged files via merkle hash. `--delta` re-indexes only files changed since last ingest (git diff → merkle fallback). `--lsp` layers LSP-confirmed CALLS.

---

## Query (6-stage hybrid retrieval → ContextBundle)

```
NL text
  ├─ fastpath?  (Class.method | TICKET-123 | path/to/file.ext) → direct lookup
  └─ translator
      1. query expansion       (CamelCase + synonym table)
      2. vector top-K          (bge-m3 → Chunk vector index)
      3. fulltext on Symbols   (Lucene, CamelCase split + escaped)
      4. RRF fusion + path-prior
      5. cross-encoder rerank  (top-30, optional)
      6. 1-hop graph expansion (IMPORTS)
            │
            ▼
      LLM grounding (file summaries + vector scores in prompt; strong-noun-match rule)
            │
            ▼
      Cypher hydration → ContextBundle (services, files, symbols, calls, runbook, decisions, observations, cross_repo)
```

PCB live (10 NL probes, Qwen3-Coder + bge-m3 + reranker on nuc):
- **Recall@1: 70%, Recall@5: 90%, MRR: 0.767**, p50 latency ~21s
- Translator picks correct file for behavior queries (e.g. "how does retry work" → `MessageRetryService`) after summary-grounding

Tunables (env, default):

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
make doctor                   # check repomix + neo4j + llm + embed
```

Per-repo config (optional):

```yaml
# .aiforge/codemem.yaml
repo: { name: my-app }
llm:   { url: http://127.0.0.1:1234/v1, model: qwen3-coder }
neo4j: { uri: bolt://127.0.0.1:7687 }
```

Read API:

```python
from aiforge_memory.api.read import context_bundle_for
print(context_bundle_for("which api saves sales data", repo="my-app"))
```

---

## CLI (full)

```
aiforge-memory ingest <repo> [--path DIR] [--force] [--delta] [--lsp]
aiforge-memory stats <repo>
aiforge-memory services <repo>

# Memory
aiforge-memory remember <repo> --type {decision|observation|note|doc} --text "..."
                                [--title ...] [--why ...] [--refs Sym1,File2]
aiforge-memory recall   <repo> --query "..." [--k N]
aiforge-memory forget   <repo> --type ... --id ID
aiforge-memory list-memory <repo> [--type ...]

# Cross-repo
aiforge-memory link --repos r1,r2,r3 [--min-confidence 0.0]
aiforge-memory link-list [--repo R]

# Eval
aiforge-memory eval <repo> --probes path.yaml [--table] [--fail-under 0.8]

# Hooks + scheduler
aiforge-memory install-hook <repo> [--path DIR]
aiforge-memory schedule add <repo> --path DIR --interval 600 [--no-pull]
aiforge-memory schedule {list|remove|run|daemon|stop|status}

# Ops
aiforge-memory doctor              # repomix + neo4j + llm
aiforge-memory health [--table]    # all sidecars + latency → ~/.aiforge/health.json
aiforge-memory ops backup [--keep 14]   # sqlite VACUUM INTO snapshot
aiforge-memory ops rotate-logs          # roll logs >10MB, keep 5 generations
```

---

## Memory layer

Code memory + conversation memory in one graph.

```bash
# Decision (+ MENTIONS edges from refs)
aiforge-memory remember PCB --type decision \
  --title "NATS over Kafka" --text "ADR-001" \
  --why "Lower ops overhead, sub-cluster ack semantics fit offline mode" \
  --refs "src/.../PosServerBackendService.java" --tags "arch,messaging"

# Observation (auto-embed for vector recall)
aiforge-memory remember PCB --type observation --kind bug \
  --text "Race in MessageRetryService when batch=50 + rebalance" \
  --refs "com.pos.backend.dataSync.MessageRetryService::pollAndRetry"

aiforge-memory recall PCB --query "race condition in retry"
aiforge-memory list-memory PCB --type decision
aiforge-memory forget PCB --type observation --id obs_abc123
```

Bundle integration: queries that hit anchor files/symbols also surface `## Decisions` and `## Observations` linked via `MENTIONS`.

---

## Cross-repo (`CALLS_REPO`)

Heuristic regex extraction over `Chunk_v2.text`:

| Signal | `via` | Confidence basis |
|---|---|---|
| HTTP client URI ↔ Spring `@RequestMapping` / FastAPI route | `http` | overlap / emitter routes |
| NATS publish subject ↔ subscribe / `@JetStreamListener` | `nats` | overlap / emitter subjects |
| `@Document(collection=…)` / `getCollection(…)` shared name | `shared_collection` | overlap / union |

```bash
aiforge-memory link --repos PCB,PSB,MongoDbService
aiforge-memory link-list --repo PCB
```

Live PCB↔PSB: 2 edges discovered (10 shared MongoDB collections @ confidence 0.54; 1 HTTP route @ 0.01).

---

## Delta ingest

```bash
aiforge-memory ingest <repo> --delta
```

Detection: git diff (preferred) → merkle hash (fallback) → cold-start (auto-fallthrough to full ingest, populates merkle for next tick).

Deletions honored (File_v2 + Symbol_v2 + Chunk_v2 detached).

---

## Scheduler daemon (production-deployed on nuc, 42 OneShell repos)

Periodic `git fetch` + `git pull --ff-only` + delta ingest, per repo.

```bash
aiforge-memory schedule add PCB --path /home/mani/codeRepo/PCB --interval 600
aiforge-memory schedule daemon       # POSIX double-fork
aiforge-memory schedule status       # JSON: pid + per-repo last_run/next_run
aiforge-memory schedule stop         # SIGTERM, waits ≤30s, SIGKILL fallback
```

`scheduler.yaml`:

```yaml
repos:
  - name: PCB
    path: /home/mani/codeRepo/PCB
    interval_seconds: 600
    pull: true                # ff-only; refuses divergence
    skip_summaries: false
    skip_chunks: false
    use_lsp: false            # opt-in LSP-confirmed CALLS
    timeout_seconds: 1800     # per-tick wall ceiling
```

Resilience:
- Per-tick wall timeout (worker thread, daemon never blocks)
- Cold-start auto-fallthrough to full ingest
- Neo4j-down classification → exp backoff (15s → 300s) + driver re-open
- Per-repo lockfile (`~/.aiforge/lock.<repo>.pid`), stale PIDs reclaimed
- Pull skipped on dirty working tree (ingest still runs via merkle)
- SIGINT/SIGTERM → in-flight delta finishes, then exit

`install-hook` writes both `post-commit` and `post-merge`. **Symlink-safe** — when an existing hook is a symlink to a shared dispatcher (e.g. AIForgeCrew's `aiforge-reindex.sh`), installer unlinks before write so it never clobbers shared infra. Hooks source `~/.aiforge/env.sh`, log to `~/.aiforge/hook.log`.

---

## Health watchdog + ops

```bash
aiforge-memory health --table        # Neo4j + LM + embed + rerank → health.json
aiforge-memory ops backup --keep 14  # sqlite VACUUM INTO snapshot
aiforge-memory ops rotate-logs       # >10MB roll, keep 5 generations
```

Recommended cron (deployed on nuc):

```cron
@reboot      sleep 30 && aiforge-memory schedule daemon
*/1 * * * *  aiforge-memory health
17 3 * * *   aiforge-memory ops backup --keep 14
0 * * * *    aiforge-memory ops rotate-logs
```

External monitors (alertmanager, ntfy, telegram) tail `~/.aiforge/health.json` and alert when `overall_ok: false` persists.

---

## LSP-confirmed CALLS (opt-in)

`--lsp` layers LSP `textDocument/references` results on top of tree-sitter heuristic. Higher confidence wins on overlap.

| Lang | Server | Status |
|---|---|---|
| Python | `pyright-langserver` (preferred), `pylsp` fallback | supported |
| TS / TSX / JS | `typescript-language-server` | supported |
| Java | `jdtls` (set `AIFORGE_JDTLS_CMD`) | experimental |

Falls back gracefully when no server installed — heuristic remains source of truth.

---

## Eval harness

Probe yaml + Recall@K + MRR + latency.

```bash
aiforge-memory eval PCB --probes probes.yaml --table
aiforge-memory eval PCB --probes probes.yaml --fail-under 0.8   # CI gate
```

```yaml
# probes.yaml
repo: PCB
probes:
  - query: "where is JWT auth handled"
    expected_files: [src/.../LogInValidationService.java]
  - query: "data sync push flow"
    expected_files: [src/.../PosServerBackendService.java]
```

---

## Operator `services.yaml` override

Skip slow Stage 3 LLM:

```yaml
# .aiforge/services.yaml
services:
  - name: data_sync
    role: consumer
    file_glob: src/main/java/com/pos/backend/dataSync/**/*.java
```

`source: 'manual'` — survives re-ingest.

---

## Required infrastructure

### CLIs

| Tool | Source | Required |
|---|---|---|
| `git`, `python3≥3.11`, `cron`, `ssh` | system | yes |
| `repomix` | `npm i -g repomix` | yes for ingest |
| `uv` | `~/.local/bin/uv` | recommended |
| `pyright-langserver`, `typescript-language-server` | npm | optional (`--lsp`) |
| `jdtls` | manual | optional (Java LSP, experimental) |

### Sidecars

| Service | Default | Required |
|---|---|---|
| Neo4j 5 (Community, Docker) | `bolt://127.0.0.1:7687` | yes |
| LM Studio (OpenAI-compat) | `:1234/v1` | yes |
| bge-m3 embed sidecar | `:8764` | yes |
| Cross-encoder reranker | `:8765` | optional |

### Runtime files

| Path | Purpose |
|---|---|
| `~/.aiforge/env.sh` | Env vars sourced by hooks/cron/daemon |
| `~/.aiforge/scheduler.yaml` | Repo schedule |
| `~/.aiforge/scheduler.{pid,status.json,log}` | Daemon state |
| `~/.aiforge/health.json` | Latest sidecar snapshot |
| `~/.aiforge/hook.log`, `logs/reindex-*.log` | Hook + per-repo logs |
| `~/.aiforge/codemem.state.db` | sqlite (merkle hashes, git_state) |
| `~/.aiforge/backups/codemem.state.*.db` | Daily snapshots (keep 14) |
| `<repo>/.git/hooks/post-{commit,merge}` | Symlink or standalone hook |

### Multi-host deploy (nuc + ms in production)

```
┌─ Mac Studio ────────┐         ┌─ NUC ──────────────────────┐
│  LM Studio :1234    │ ──SSH-R─┤  scheduler daemon          │
│  127.0.0.1 only     │         │  cron (@reboot/health/...) │
└─────────────────────┘         │  Neo4j (docker)            │
                                │  bge-m3 + reranker         │
                                │  ~/.aiforge/* + 42 repos   │
                                └────────────────────────────┘
```

Steps: SSH reverse tunnel ms→nuc :1234, `uv pip install -e .[dev]`, generate `scheduler.yaml`, write `env.sh`, `install-hook` per repo, `schedule daemon`, install crons.

---

## Test gates (`make test-L<N>`)

| Gate | What | Backend |
|---|---|---|
| L1 | Repo node | RepoMix + LLM |
| L2 | Services + override | LLM + glob |
| L3 | File summaries | LLM |
| L4 | Symbols + CALLS | tree-sitter |
| L5 | Chunks | bge-m3 |
| L6 | Translator | NL → entities |
| L7 | Bundle | full pipeline |
| L8 | Memory layer (Decision/Observation/Note/Doc) | sqlite + Neo4j |
| L9 | Cross-repo `CALLS_REPO` extraction | pure-python |
| L10 | Delta ingest (git + merkle) | pure-python |
| L11 | Eval harness | pure-python |
| L12 | Scheduler config + lock + git poll | pure-python |
| L13 | Symbol enrichment | tree-sitter fixtures |
| L14 | LSP wire codec + adapter + resolver | pure-python |
| L15 | git_meta against ephemeral repo | git CLI |

`make test` → **182 passing** (full live infra), `make test-unit` → no-infra subset, ~1s.

---

## Real PCB graph

| | Count |
|---|---|
| Files | 1,030 |
| Symbols | 9,176 (4187 method, 3885 field, 750 class, 351 interface) |
| Chunks | 2,759 |
| CALLS | 12,273 (16% conf 1.0 / 31% 0.7 / 53% 0.4 — LSP can lift 0.4 tier) |
| IMPORTS | 2,190 |
| CALLS_REPO (PCB↔PSB) | 2 (10 shared collections + 1 HTTP route) |

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Worktree pollution in queries | Ingest filters `.aiforge-worktrees/**`; existing graphs: `MATCH (n) WHERE n.path STARTS WITH ".aiforge-worktrees/" DETACH DELETE n` |
| Translator returns nothing | Check `AIFORGE_INTENT_LM_URL` (default `:1235` ≠ agent `:1234`) |
| Rerank slow / wrong | `AIFORGE_TRANSLATOR_RERANK=0` (RRF fallback) or tune `_TOPN` |
| Hook fired but no index | `tail ~/.aiforge/hook.log`; check `env.sh` exists; check sidecars (`aiforge-memory health`) |
| Daemon won't restart (`already running`) | `pkill -9 -f "aiforge-memory schedule" && rm ~/.aiforge/scheduler.pid` |
| One slow ingest blocks loop | Lower `timeout_seconds` for that repo in `scheduler.yaml` |
| LM Studio remote down | Health probe alerts within 60s; daemon backoff handles auto-reconnect |
| State DB lost | `cp ~/.aiforge/backups/codemem.state.<latest>.db ~/.aiforge/codemem.state.db` |
| Neo4j eats memory on PCB-scale graph | Bump heap: `NEO4J_dbms_memory_heap_max__size: "4G"` |
| `cold_start_required` on every tick | Pre-existing graph indexed before merkle was written; one full `--force` ingest fixes it |

---

## Coexistence

`_v2`-suffixed labels (`File_v2`, `Symbol_v2`, `Chunk_v2`, `Decision_v2`, etc.) avoid colliding with other Neo4j tenants. Drop suffix in single migration once sole tenant.

```cypher
MATCH (n) WHERE n.schema_version = 'codemem-v1' DETACH DELETE n
```

---

## Origin

Extracted from [AIForgeCrew](https://github.com/Manikanta-Reddy-Pasala/AIForgeCrew). MIT.
