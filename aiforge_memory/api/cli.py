"""`aiforge-memory` operator CLI.

Subcommands:
    aiforge-memory ingest <repo> [--path DIR] [--force] [--delta]
    aiforge-memory doctor
    aiforge-memory stats <repo>
    aiforge-memory services <repo>
    aiforge-memory remember <repo> --type {decision|observation|note}
                              --text "..." [--title ...] [--why ...]
                              [--refs Symbol1,File2] [--tags a,b]
    aiforge-memory recall <repo> --query "..." [--type ...] [--k N]
    aiforge-memory forget <repo> --id ID --type {decision|observation|note|doc}
    aiforge-memory list-memory <repo> [--type ...] [--limit N]
    aiforge-memory link --repos r1,r2,r3 [--min-confidence 0.0]
    aiforge-memory link-list [--repo R]
    aiforge-memory eval <repo> --probes path.yaml [--table] [--budget N]
    aiforge-memory install-hook <repo> [--path DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from aiforge_memory.ingest import delta, flow, link, scheduler
from aiforge_memory.ops import backup as ops_backup
from aiforge_memory.ops import health as ops_health
from aiforge_memory.store import (
    link_writer,
    memory_writer,
    schema,
)
from aiforge_memory.store import (
    state_db as sdb,
)


def _driver():
    """Open the project's Neo4j driver. Errors propagate to caller."""
    from neo4j import GraphDatabase
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    return GraphDatabase.driver(uri, auth=(user, pw))


def _cmd_ingest(args: argparse.Namespace) -> int:
    from dataclasses import asdict, is_dataclass

    from aiforge_memory.config import RepoConfig

    repo_path = args.path or os.getcwd()
    cfg = RepoConfig.load(repo_path, name=args.repo)
    cfg.apply_to_env()  # so legacy modules pick up overrides

    drv = _driver()
    schema.apply(drv)
    state = sdb.open_db()
    sdb.migrate(state)

    if args.delta:
        res = delta.ingest_delta(
            repo_name=cfg.name, repo_path=cfg.path,
            driver=drv, state_conn=state,
            skip_summaries=cfg.skip_summaries,
            skip_chunks=cfg.skip_chunks,
            use_lsp=args.lsp,
        )
        if res.status == "cold_start_required":
            # Auto-fall-through to full ingest on first run.
            res = flow.ingest_repo(
                repo_name=cfg.name, repo_path=cfg.path,
                driver=drv, state_conn=state, force=False,
                skip_services=cfg.skip_services,
                skip_symbols=cfg.skip_symbols,
                skip_summaries=cfg.skip_summaries,
                skip_chunks=cfg.skip_chunks,
                use_lsp=args.lsp,
            )
    else:
        res = flow.ingest_repo(
            repo_name=cfg.name,
            repo_path=cfg.path,
            driver=drv,
            state_conn=state,
            force=args.force,
            skip_services=cfg.skip_services,
            skip_symbols=cfg.skip_symbols,
            skip_summaries=cfg.skip_summaries,
            skip_chunks=cfg.skip_chunks,
            use_lsp=args.lsp,
        )
    if is_dataclass(res):
        base = asdict(res)
    else:
        # Defensive: tests may stub ingest with a plain object.
        base = {k: getattr(res, k) for k in (
            "status", "pack_sha", "repo",
        ) if hasattr(res, k)}
    payload = {
        **base,
        "config_loaded_from": str(
            (Path(repo_path) / ".aiforge" / "codemem.yaml").resolve()
        ),
    }
    print(json.dumps(payload, default=str, indent=2))
    return 0


def _cmd_services(args: argparse.Namespace) -> int:
    drv = _driver()
    with drv.session() as s:
        rows = list(s.run(
            "MATCH (r:Repo {name:$n})-[:OWNS_SERVICE]->(s:Service) "
            "OPTIONAL MATCH (s)-[:CONTAINS_FILE]->(f:File_v2) "
            "WITH s, count(f) AS file_count "
            "RETURN s.name AS name, s.role AS role, s.port AS port, "
            "       s.source AS source, s.tech_stack AS tech_stack, "
            "       file_count, s.description AS description "
            "ORDER BY s.name", n=args.repo,
        ))
    services = [dict(r) for r in rows]
    if not services:
        print(json.dumps({"repo": args.repo, "services": []}))
        return 0 if args.allow_empty else 1
    print(json.dumps({"repo": args.repo, "services": services}, indent=2))
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    drv = _driver()
    with drv.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:$n}) RETURN r", n=args.repo
        ).single()
    if not row:
        print(json.dumps({"error": "repo_not_found", "repo": args.repo}))
        return 1
    r = dict(row["r"])
    if "last_indexed_at" in r and r["last_indexed_at"] is not None:
        r["last_indexed_at"] = str(r["last_indexed_at"])
    runbook = r.pop("runbook_md", "") or ""
    conventions = r.pop("conventions_md", "") or ""
    r["runbook_md_chars"] = len(runbook)
    r["conventions_md_chars"] = len(conventions)
    print(json.dumps(r, indent=2, default=str))
    return 0


def _check_repomix() -> tuple[bool, str]:
    binary = os.environ.get("AIFORGE_CODEMEM_REPOMIX", "repomix")
    path = shutil.which(binary)
    if not path:
        return False, f"{binary} not on PATH"
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception as exc:
        return False, str(exc)
    return True, proc.stdout.strip() or "ok"


def _check_neo4j() -> tuple[bool, str]:
    try:
        drv = _driver()
        with drv.session() as s:
            s.run("RETURN 1").consume()
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _check_llm() -> tuple[bool, str]:
    import urllib.error
    import urllib.request
    url = os.environ.get(
        "AIFORGE_CODEMEM_LM_URL",
        os.environ.get("AIFORGE_INTENT_LM_URL", "http://127.0.0.1:1235/v1"),
    )
    probe = url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(probe, timeout=3) as resp:
            ok = resp.status == 200
        return (True, "ok") if ok else (False, f"status {resp.status}")
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("repomix", _check_repomix()),
        ("neo4j", _check_neo4j()),
        ("llm", _check_llm()),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "info": info}
                          for n, (ok, info) in checks]}
    print(json.dumps(payload, indent=2))
    return 0 if all(ok for _, (ok, _) in checks) else 1


# ─── Memory commands ──────────────────────────────────────────────────

def _embed_text(text: str) -> list[float] | None:
    """Best-effort: embed via the bge-m3 sidecar. Returns None on failure
    so memory writes degrade gracefully when the sidecar is offline."""
    import urllib.error
    import urllib.request

    url = os.environ.get("AIFORGE_EMBED_URL", "http://127.0.0.1:8764").rstrip("/")
    body = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        url + "/embed", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        vec = data.get("embedding") or []
        return [float(x) for x in vec] if vec else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _split_csv(s: str | None) -> list[str]:
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


def _cmd_remember(args: argparse.Namespace) -> int:
    drv = _driver()
    refs = _split_csv(args.refs)
    tags = _split_csv(args.tags)
    if args.type == "decision":
        out = memory_writer.upsert_decision(
            drv, repo=args.repo,
            title=args.title or args.text[:80],
            body=args.text, rationale=args.why or "",
            status=args.status, author=args.author, session_id=args.session,
            tags=tags, refs=refs,
            supersedes_id=args.supersedes,
        )
    elif args.type == "observation":
        vec = _embed_text(args.text) if not args.no_embed else None
        out = memory_writer.upsert_observation(
            drv, repo=args.repo, text=args.text,
            kind=args.kind or "note", author=args.author,
            session_id=args.session, tags=tags, refs=refs,
            embed_vec=vec,
        )
        out["embedded"] = vec is not None
    elif args.type == "note":
        out = memory_writer.upsert_note(
            drv, repo=args.repo,
            title=args.title or args.text[:80],
            body=args.text, author=args.author,
            tags=tags, refs=refs,
        )
    elif args.type == "doc":
        out = memory_writer.upsert_doc(
            drv, repo=args.repo,
            title=args.title or args.text[:80],
            body=args.text, url=args.url or "",
            source_kind=args.kind or "web", refs=refs,
        )
    else:
        print(json.dumps({"error": "unknown type"}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


def _cmd_recall(args: argparse.Namespace) -> int:
    drv = _driver()
    vec = _embed_text(args.query)
    if vec is None:
        print(json.dumps({
            "error": "embed sidecar unreachable", "query": args.query,
        }))
        return 2
    rows = memory_writer.recall_observations(
        drv, repo=args.repo, query_vec=vec, k=args.k,
    )
    print(json.dumps({"repo": args.repo, "results": rows}, indent=2))
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    label_map = {
        "decision": "Decision_v2", "observation": "Observation_v2",
        "note": "Note_v2", "doc": "Doc_v2",
    }
    label = label_map.get(args.type)
    if not label:
        print(json.dumps({"error": "unknown type"}))
        return 2
    drv = _driver()
    res = memory_writer.forget(drv, repo=args.repo, node_id=args.id, label=label)
    print(json.dumps(res, indent=2))
    return 0 if res.get("deleted") else 1


def _cmd_list_memory(args: argparse.Namespace) -> int:
    label_map = {
        "decision": "Decision_v2", "observation": "Observation_v2",
        "note": "Note_v2", "doc": "Doc_v2",
    }
    label = label_map.get(args.type) if args.type else None
    drv = _driver()
    rows = memory_writer.list_memory(
        drv, repo=args.repo, label=label, limit=args.limit,
    )
    print(json.dumps({"repo": args.repo, "memory": rows}, indent=2))
    return 0


# ─── Cross-repo link commands ─────────────────────────────────────────

def _cmd_link(args: argparse.Namespace) -> int:
    repos = _split_csv(args.repos)
    if len(repos) < 2:
        print(json.dumps({"error": "need at least 2 repos via --repos"}))
        return 2
    drv = _driver()
    counts = link.run(drv, repos=repos, min_confidence=args.min_confidence)
    print(json.dumps(counts, indent=2))
    return 0


def _cmd_link_list(args: argparse.Namespace) -> int:
    drv = _driver()
    rows = link_writer.list_edges(drv, repo=args.repo)
    print(json.dumps({"edges": rows}, indent=2))
    return 0


# ─── Eval ─────────────────────────────────────────────────────────────

def _cmd_eval(args: argparse.Namespace) -> int:
    from aiforge_memory.eval import harness as ev

    drv = _driver()
    report = ev.run_eval(
        probes_path=args.probes, driver=drv,
        repo=args.repo, token_budget=args.budget,
    )
    if args.table:
        print(ev.render_table(report))
    else:
        print(ev.report_to_json(report))
    # exit non-zero if Recall@5 below threshold
    if args.fail_under is not None and report.recall_at_5 < args.fail_under:
        return 1
    return 0


# ─── Hook installer ───────────────────────────────────────────────────

def _cmd_install_hook(args: argparse.Namespace) -> int:
    repo_path = args.path or os.getcwd()
    try:
        commit_hook = delta.install_post_commit_hook(repo_path, args.repo)
        merge_hook = delta.install_post_merge_hook(repo_path, args.repo)
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps({
        "installed_post_commit": str(commit_hook),
        "installed_post_merge": str(merge_hook),
    }, indent=2))
    return 0


# ─── Scheduler ────────────────────────────────────────────────────────

def _cmd_schedule_add(args: argparse.Namespace) -> int:
    rs = scheduler.RepoSchedule(
        name=args.repo,
        path=str(Path(args.path or os.getcwd()).resolve()),
        interval_seconds=args.interval,
        pull=not args.no_pull,
        skip_summaries=args.skip_summaries,
        skip_chunks=args.skip_chunks,
    )
    scheduler.add_repo(rs)
    print(json.dumps({"added": rs.__dict__,
                      "config": str(scheduler.CONFIG_PATH)}, indent=2))
    return 0


def _cmd_schedule_remove(args: argparse.Namespace) -> int:
    ok = scheduler.remove_repo(args.repo)
    print(json.dumps({"removed": ok, "repo": args.repo}))
    return 0 if ok else 1


def _cmd_schedule_list(_args: argparse.Namespace) -> int:
    cfg = scheduler.SchedulerConfig.load()
    print(json.dumps({
        "config": str(scheduler.CONFIG_PATH),
        "repos": [r.__dict__ for r in cfg.repos],
    }, indent=2))
    return 0


def _cmd_schedule_run(args: argparse.Namespace) -> int:
    scheduler.run_loop(once=args.once)
    return 0


def _cmd_schedule_daemon(_args: argparse.Namespace) -> int:
    pid = scheduler.daemonize()
    print(json.dumps({"daemon_pid": pid,
                      "log": str(scheduler.LOG_PATH)}, indent=2))
    return 0 if pid > 0 else 1


def _cmd_schedule_stop(_args: argparse.Namespace) -> int:
    ok = scheduler.stop_daemon()
    print(json.dumps({"stopped": ok}))
    return 0 if ok else 1


def _cmd_schedule_status(_args: argparse.Namespace) -> int:
    print(json.dumps(scheduler.daemon_status(), indent=2, default=str))
    return 0


# ─── Health watchdog ──────────────────────────────────────────────────

def _cmd_health(args: argparse.Namespace) -> int:
    report = ops_health.check_all()
    ops_health.write_snapshot(report)
    if args.table:
        print(ops_health.render_table(report))
    else:
        from dataclasses import asdict
        print(json.dumps({
            "ts": report.ts,
            "overall_ok": report.overall_ok,
            "checks": [asdict(c) for c in report.checks],
        }, indent=2))
    return 0 if report.overall_ok else 1


# ─── Ops (backup + log rotate) ────────────────────────────────────────

def _cmd_ops_backup(args: argparse.Namespace) -> int:
    res = ops_backup.backup_state()
    rotated = ops_backup.rotate_backups(keep=args.keep)
    print(json.dumps({
        "backed_up": res.backed_up,
        "rotated_out": rotated.rotated_out,
        "errors": res.errors + rotated.errors,
    }, indent=2))
    return 0 if not (res.errors or rotated.errors) else 1


def _cmd_ops_rotate_logs(_args: argparse.Namespace) -> int:
    out = ops_backup.rotate_known_logs()
    print(json.dumps({"rotated": out}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aiforge-memory")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Stage 1+2 ingest of a repo")
    ing.add_argument("repo", help="Logical repo name (becomes Repo.name)")
    ing.add_argument("--path", help="Repo dir; defaults to CWD")
    ing.add_argument("--force", action="store_true",
                     help="Re-run even if pack_sha matches")
    ing.add_argument("--delta", action="store_true",
                     help="Re-index only files changed since last ingest")
    ing.add_argument("--lsp", action="store_true",
                     help="Layer LSP-confirmed CALLS on top of tree-sitter "
                          "heuristic (per-language adapter required on PATH)")
    ing.set_defaults(func=_cmd_ingest)

    st = sub.add_parser("stats", help="Print Repo node summary")
    st.add_argument("repo")
    st.set_defaults(func=_cmd_stats)

    sv = sub.add_parser("services", help="List services for a repo")
    sv.add_argument("repo")
    sv.add_argument("--allow-empty", action="store_true",
                    help="exit 0 even when no services found")
    sv.set_defaults(func=_cmd_services)

    doc = sub.add_parser("doctor", help="Check repomix, neo4j, llm")
    doc.set_defaults(func=_cmd_doctor)

    # ─── Memory ───────────────────────────────────────────────────────
    rem = sub.add_parser("remember", help="Record a memory node")
    rem.add_argument("repo")
    rem.add_argument("--type", choices=["decision", "observation", "note", "doc"],
                     required=True)
    rem.add_argument("--text", required=True, help="Body / observation text")
    rem.add_argument("--title", help="Title (decision/note/doc)")
    rem.add_argument("--why", help="Rationale (decision)")
    rem.add_argument("--status", default="active",
                     help="Decision status: active|superseded|rejected")
    rem.add_argument("--kind", help="Observation kind / Doc source_kind")
    rem.add_argument("--author", default="",
                     help="Author identifier (agent / user)")
    rem.add_argument("--session", default="",
                     help="Session id for grouping memories")
    rem.add_argument("--tags", help="comma-separated tags")
    rem.add_argument("--refs",
                     help="comma-separated Symbol fqnames or File paths")
    rem.add_argument("--supersedes",
                     help="Decision id this one supersedes")
    rem.add_argument("--url", help="Doc source URL")
    rem.add_argument("--no-embed", action="store_true",
                     help="Skip embedding even for observation")
    rem.set_defaults(func=_cmd_remember)

    rec = sub.add_parser("recall", help="Vector recall over Observations")
    rec.add_argument("repo")
    rec.add_argument("--query", required=True)
    rec.add_argument("--k", type=int, default=10)
    rec.set_defaults(func=_cmd_recall)

    fgt = sub.add_parser("forget", help="Hard-delete a memory node by id")
    fgt.add_argument("repo")
    fgt.add_argument("--id", required=True)
    fgt.add_argument("--type", choices=["decision", "observation", "note", "doc"],
                     required=True)
    fgt.set_defaults(func=_cmd_forget)

    lm = sub.add_parser("list-memory", help="List memory nodes for a repo")
    lm.add_argument("repo")
    lm.add_argument("--type",
                    choices=["decision", "observation", "note", "doc"])
    lm.add_argument("--limit", type=int, default=50)
    lm.set_defaults(func=_cmd_list_memory)

    # ─── Cross-repo link ──────────────────────────────────────────────
    lk = sub.add_parser("link", help="Compute cross-repo CALLS_REPO edges")
    lk.add_argument("--repos", required=True,
                    help="comma-separated repo names")
    lk.add_argument("--min-confidence", type=float, default=0.0)
    lk.set_defaults(func=_cmd_link)

    ll = sub.add_parser("link-list", help="List CALLS_REPO edges")
    ll.add_argument("--repo", help="filter to edges touching this repo")
    ll.set_defaults(func=_cmd_link_list)

    # ─── Eval ─────────────────────────────────────────────────────────
    ev = sub.add_parser("eval", help="Run NL probe eval against a repo")
    ev.add_argument("repo", nargs="?", default=None,
                    help="overrides probes.yaml repo when given")
    ev.add_argument("--probes", required=True, help="path to probes yaml")
    ev.add_argument("--budget", type=int, default=4000,
                    help="ContextBundle token budget")
    ev.add_argument("--table", action="store_true", help="markdown table")
    ev.add_argument("--fail-under", type=float, default=None,
                    help="exit 1 if Recall@5 below this threshold")
    ev.set_defaults(func=_cmd_eval)

    # ─── Hook installer ───────────────────────────────────────────────
    ih = sub.add_parser(
        "install-hook",
        help="Install git post-commit + post-merge hooks for delta ingest",
    )
    ih.add_argument("repo")
    ih.add_argument("--path", help="Repo dir; defaults to CWD")
    ih.set_defaults(func=_cmd_install_hook)

    # ─── Scheduler ────────────────────────────────────────────────────
    sc = sub.add_parser(
        "schedule",
        help="Periodic git fetch/pull + delta ingest daemon",
    )
    sc_sub = sc.add_subparsers(dest="schedule_cmd", required=True)

    sc_add = sc_sub.add_parser("add", help="Add a repo to the schedule")
    sc_add.add_argument("repo")
    sc_add.add_argument("--path", help="Repo dir; defaults to CWD")
    sc_add.add_argument("--interval", type=int, default=600,
                        help="poll interval in seconds (default 600)")
    sc_add.add_argument("--no-pull", action="store_true",
                        help="fetch only — do not run git pull --ff-only")
    sc_add.add_argument("--skip-summaries", action="store_true")
    sc_add.add_argument("--skip-chunks", action="store_true")
    sc_add.set_defaults(func=_cmd_schedule_add)

    sc_rm = sc_sub.add_parser("remove", help="Remove a repo from the schedule")
    sc_rm.add_argument("repo")
    sc_rm.set_defaults(func=_cmd_schedule_remove)

    sc_ls = sc_sub.add_parser("list", help="List scheduled repos")
    sc_ls.set_defaults(func=_cmd_schedule_list)

    sc_run = sc_sub.add_parser("run",
                               help="Run loop in foreground (Ctrl-C to stop)")
    sc_run.add_argument("--once", action="store_true",
                        help="single tick over each repo, then exit")
    sc_run.set_defaults(func=_cmd_schedule_run)

    sc_dm = sc_sub.add_parser("daemon",
                              help="Fork into background (POSIX)")
    sc_dm.set_defaults(func=_cmd_schedule_daemon)

    sc_st = sc_sub.add_parser("stop", help="Stop the running daemon (SIGTERM)")
    sc_st.set_defaults(func=_cmd_schedule_stop)

    sc_status = sc_sub.add_parser("status",
                                  help="JSON: pid + per-repo last_run / next_run")
    sc_status.set_defaults(func=_cmd_schedule_status)

    # ─── Health ───────────────────────────────────────────────────────
    hc = sub.add_parser("health",
                        help="Probe Neo4j + LM + embed + rerank sidecars")
    hc.add_argument("--table", action="store_true")
    hc.set_defaults(func=_cmd_health)

    # ─── Ops (backup, log rotate) ─────────────────────────────────────
    ops = sub.add_parser("ops",
                         help="Operational helpers: backup + log rotation")
    ops_sub = ops.add_subparsers(dest="ops_cmd", required=True)

    ops_b = ops_sub.add_parser("backup",
                               help="VACUUM INTO snapshot of state.db; rotates oldest")
    ops_b.add_argument("--keep", type=int, default=7)
    ops_b.set_defaults(func=_cmd_ops_backup)

    ops_r = ops_sub.add_parser("rotate-logs",
                               help="Rotate AiForge logs over 10MB")
    ops_r.set_defaults(func=_cmd_ops_rotate_logs)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
