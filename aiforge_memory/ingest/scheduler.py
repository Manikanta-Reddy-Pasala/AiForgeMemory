"""Scheduler daemon — periodic git fetch + delta ingest per repo.

Reads `~/.aiforge/scheduler.yaml`:

    repos:
      - name: PosClientBackend
        path: /Users/me/code/pcb
        interval_seconds: 600          # default 600
        pull: true                      # ff-only pull from origin
        skip_summaries: false
        skip_chunks: false
      - name: PosServerBackend
        path: /Users/me/code/psb
        interval_seconds: 1800

Run modes:
    aiforge-memory schedule run        # foreground; Ctrl-C to stop
    aiforge-memory schedule daemon     # background fork; pidfile in
                                       # ~/.aiforge/scheduler.pid
    aiforge-memory schedule status     # JSON: per-repo last_run, next_run
    aiforge-memory schedule add        # mutate yaml
    aiforge-memory schedule remove
    aiforge-memory schedule list

Safety:
    - `git pull --ff-only` only — refuses on divergence (no rebase, no merge).
    - Per-repo lockfile prevents overlapping runs.
    - SIGINT / SIGTERM handled cleanly; in-flight delta finishes.
    - If repo's working tree is dirty (tracked-file mods), the pull is
      skipped and a warning logged; ingest still runs to capture local
      uncommitted state via merkle fallback.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_CONFIG",
        os.path.expanduser("~/.aiforge/scheduler.yaml"),
    )
)
PID_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_PIDFILE",
        os.path.expanduser("~/.aiforge/scheduler.pid"),
    )
)
STATUS_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_STATUS",
        os.path.expanduser("~/.aiforge/scheduler.status.json"),
    )
)
LOG_PATH = Path(
    os.environ.get(
        "AIFORGE_SCHEDULER_LOG",
        os.path.expanduser("~/.aiforge/scheduler.log"),
    )
)


# ─── Config ───────────────────────────────────────────────────────────

@dataclass
class RepoSchedule:
    name: str
    path: str
    interval_seconds: int = 600
    pull: bool = True
    skip_summaries: bool = False
    skip_chunks: bool = False
    use_lsp: bool = False          # opt-in LSP-confirmed CALLS
    timeout_seconds: int = 1800    # per-tick wall ceiling (prevents one
                                   # 70-min ingest blocking the loop)


@dataclass
class SchedulerConfig:
    repos: list[RepoSchedule] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> SchedulerConfig:
        path = Path(path or CONFIG_PATH)
        if not path.is_file():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            return cls()
        repos: list[RepoSchedule] = []
        for r in data.get("repos") or []:
            try:
                repos.append(RepoSchedule(
                    name=str(r["name"]),
                    path=str(r["path"]),
                    interval_seconds=int(r.get("interval_seconds", 600)),
                    pull=bool(r.get("pull", True)),
                    skip_summaries=bool(r.get("skip_summaries", False)),
                    skip_chunks=bool(r.get("skip_chunks", False)),
                    use_lsp=bool(r.get("use_lsp", False)),
                    timeout_seconds=int(r.get("timeout_seconds", 1800)),
                ))
            except (KeyError, ValueError):
                continue
        return cls(repos=repos)

    def save(self, path: Path | None = None) -> None:
        path = Path(path or CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(
            {"repos": [asdict(r) for r in self.repos]},
            default_flow_style=False, sort_keys=False,
        ))


def add_repo(rs: RepoSchedule, *, path: Path | None = None) -> None:
    cfg = SchedulerConfig.load(path)
    cfg.repos = [r for r in cfg.repos if r.name != rs.name]
    cfg.repos.append(rs)
    cfg.save(path)


def remove_repo(name: str, *, path: Path | None = None) -> bool:
    cfg = SchedulerConfig.load(path)
    before = len(cfg.repos)
    cfg.repos = [r for r in cfg.repos if r.name != name]
    cfg.save(path)
    return len(cfg.repos) < before


# ─── Git helpers (poll-decide) ────────────────────────────────────────

@dataclass
class FetchOutcome:
    fetched: bool                  # `git fetch` succeeded
    pulled: bool                   # `git pull --ff-only` succeeded
    behind: int                    # commits behind upstream BEFORE pull
    skipped_reason: str = ""       # 'dirty' | 'no_upstream' | ''


def fetch_and_maybe_pull(
    repo_path: str | Path, *, do_pull: bool = True,
) -> FetchOutcome:
    """git fetch; report ahead/behind; ff-only pull if behind & clean.

    - Refuses to pull when working tree is dirty.
    - Refuses to pull when upstream is not configured.
    - ff-only — won't merge or rebase divergent histories.
    """
    cwd = str(Path(repo_path).resolve())

    fetch_ok = _git_run(cwd, "fetch", "--quiet").returncode == 0
    if not fetch_ok:
        return FetchOutcome(False, False, 0)

    behind = _commits_behind(cwd)
    if behind <= 0:
        return FetchOutcome(True, False, 0)

    if not do_pull:
        return FetchOutcome(True, False, behind)

    if _is_dirty(cwd):
        return FetchOutcome(True, False, behind, skipped_reason="dirty")

    if not _has_upstream(cwd):
        return FetchOutcome(True, False, behind, skipped_reason="no_upstream")

    pull = _git_run(cwd, "pull", "--ff-only", "--quiet")
    return FetchOutcome(True, pull.returncode == 0, behind)


def _git_run(cwd: str, *args: str, timeout: int = 30):
    return subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _commits_behind(cwd: str) -> int:
    """Count of commits HEAD is behind @{u}. 0 if up-to-date or unknown."""
    r = _git_run(cwd, "rev-list", "--count", "HEAD..@{u}")
    if r.returncode != 0:
        return 0
    try:
        return int((r.stdout or "0").strip())
    except ValueError:
        return 0


def _is_dirty(cwd: str) -> bool:
    r = _git_run(cwd, "status", "--porcelain=v1", "-uno")
    return bool((r.stdout or "").strip())


def _has_upstream(cwd: str) -> bool:
    return _git_run(
        cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
    ).returncode == 0


# ─── Per-repo lockfile ────────────────────────────────────────────────

def _lockfile(name: str) -> Path:
    return Path(os.path.expanduser(f"~/.aiforge/lock.{_safe(name)}.pid"))


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)


def _acquire_lock(name: str) -> bool:
    lf = _lockfile(name)
    if lf.exists():
        try:
            pid = int(lf.read_text().strip())
            os.kill(pid, 0)              # signal 0 = check if alive
            return False                 # still alive — locked
        except (ValueError, OSError, ProcessLookupError):
            pass                         # stale; reclaim
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(str(os.getpid()))
    return True


def _release_lock(name: str) -> None:
    try:
        _lockfile(name).unlink()
    except FileNotFoundError:
        pass


# ─── Status journal ───────────────────────────────────────────────────

@dataclass
class RepoStatus:
    name: str
    last_run: float = 0.0          # unix ts
    last_status: str = ""          # 'delta_applied'|'no_changes'|'error'|...
    last_pulled: bool = False
    last_behind: int = 0
    last_error: str = ""
    next_run: float = 0.0


def _read_status() -> dict[str, RepoStatus]:
    if not STATUS_PATH.is_file():
        return {}
    try:
        raw = json.loads(STATUS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, RepoStatus] = {}
    for name, d in (raw or {}).items():
        out[name] = RepoStatus(name=name, **{
            k: d.get(k, getattr(RepoStatus(name=name), k))
            for k in ("last_run", "last_status", "last_pulled",
                      "last_behind", "last_error", "next_run")
        })
    return out


def _write_status(d: dict[str, RepoStatus]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(
        {n: asdict(s) for n, s in d.items()}, indent=2,
    ))


# ─── Tick ──────────────────────────────────────────────────────────────

def tick_repo(
    rs: RepoSchedule, *, driver, state_conn, log,
) -> RepoStatus:
    """Run one fetch + delta cycle for a repo. Returns RepoStatus update.

    Resilience:
      - Per-tick wall timeout (rs.timeout_seconds) — runs the ingest in
        a worker thread, joins with timeout. Long-running stages can't
        block the loop forever.
      - Neo4j connection errors set status='neo4j_down' so a watchdog
        can react. Caller may apply exponential backoff.
      - LSP opt-in via rs.use_lsp.
    """
    import threading

    from aiforge_memory.ingest import delta, flow

    status = RepoStatus(name=rs.name)
    status.last_run = time.time()
    status.next_run = status.last_run + rs.interval_seconds

    if not _acquire_lock(rs.name):
        status.last_status = "locked"
        log(f"[{rs.name}] previous run still active; skipped")
        return status

    result_box: dict = {"res": None, "exc": None, "out": None}

    def _work() -> None:
        try:
            out = fetch_and_maybe_pull(rs.path, do_pull=rs.pull)
            result_box["out"] = out
            res = delta.ingest_delta(
                repo_name=rs.name, repo_path=rs.path,
                driver=driver, state_conn=state_conn,
                skip_summaries=rs.skip_summaries,
                skip_chunks=rs.skip_chunks,
                use_lsp=rs.use_lsp,
            )
            if res.status == "cold_start_required":
                log(f"[{rs.name}] cold_start_required → running full ingest")
                res = flow.ingest_repo(
                    repo_name=rs.name, repo_path=rs.path,
                    driver=driver, state_conn=state_conn, force=False,
                    skip_summaries=rs.skip_summaries,
                    skip_chunks=rs.skip_chunks,
                    use_lsp=rs.use_lsp,
                )
            result_box["res"] = res
        except Exception as exc:  # noqa: BLE001 — must surface to outer
            result_box["exc"] = exc

    try:
        worker = threading.Thread(target=_work, name=f"tick-{rs.name}",
                                  daemon=True)
        worker.start()
        worker.join(timeout=rs.timeout_seconds)
        if worker.is_alive():
            status.last_status = "timeout"
            status.last_error = f"tick exceeded {rs.timeout_seconds}s"
            log(f"[{rs.name}] timeout after {rs.timeout_seconds}s — "
                "thread leaked (will be cleaned up by GC)")
            # Worker thread keeps running but daemon=True so it dies on
            # process exit. Lock release below frees subsequent ticks.
            return status

        if result_box["exc"] is not None:
            exc = result_box["exc"]
            err_text = str(exc)
            # Classify Neo4j-down errors so watchdogs can react.
            if any(s in err_text.lower() for s in (
                "service unavailable", "session expired",
                "connection refused", "unable to retrieve routing",
            )):
                status.last_status = "neo4j_down"
            else:
                status.last_status = "error"
            status.last_error = err_text[:240]
            log(f"[{rs.name}] {status.last_status}: {exc!r}")
            return status

        out = result_box["out"]
        res = result_box["res"]
        status.last_pulled = out.pulled if out else False
        status.last_behind = out.behind if out else 0
        if out and out.skipped_reason:
            log(f"[{rs.name}] pull skipped: {out.skipped_reason}; "
                f"behind={out.behind}")
        status.last_status = res.status
        log(f"[{rs.name}] {res.status} files={res.files_count} "
            f"symbols={res.symbols_count} chunks={res.chunks_count} "
            f"pulled={status.last_pulled} behind={status.last_behind} "
            f"lsp={rs.use_lsp}")
    finally:
        _release_lock(rs.name)

    return status


# ─── Loop ─────────────────────────────────────────────────────────────

class _StopFlag:
    """Cooperative shutdown flag toggled by SIGINT/SIGTERM."""
    def __init__(self) -> None:
        self.stop = False

    def trip(self, *_args) -> None:
        self.stop = True


def run_loop(
    *,
    config: SchedulerConfig | None = None,
    driver_factory=None,
    state_factory=None,
    log_path: Path | None = None,
    once: bool = False,
) -> None:
    """Foreground scheduler loop. ``once=True`` runs each repo a single
    time and exits — useful for cron + tests."""
    cfg = config or SchedulerConfig.load()
    if not cfg.repos:
        _log_to(log_path, "no repos configured in scheduler.yaml; exiting")
        return

    driver = (driver_factory or _default_driver)()
    state_conn = (state_factory or _default_state)()

    flag = _StopFlag()
    signal.signal(signal.SIGINT, flag.trip)
    signal.signal(signal.SIGTERM, flag.trip)

    # Per-repo "next-due" timestamps, all initially due now.
    due: dict[str, float] = {r.name: 0.0 for r in cfg.repos}
    statuses = _read_status()

    def log(msg: str) -> None:
        _log_to(log_path, msg)

    # Exponential-backoff wait when Neo4j keeps failing across the loop.
    # Caps at 5 minutes so we keep retrying but don't spam logs / drivers.
    backoff_seconds = 0
    BACKOFF_MAX = 300

    while not flag.stop:
        now = time.time()
        any_neo4j_down = False
        for rs in cfg.repos:
            if flag.stop:
                break
            if now < due.get(rs.name, 0):
                continue
            st = tick_repo(
                rs, driver=driver, state_conn=state_conn, log=log,
            )
            statuses[rs.name] = st
            due[rs.name] = st.next_run
            _write_status(statuses)
            if st.last_status == "neo4j_down":
                any_neo4j_down = True

        if any_neo4j_down:
            backoff_seconds = min(BACKOFF_MAX,
                                  max(15, backoff_seconds * 2 or 15))
            log(f"neo4j_down detected; backing off {backoff_seconds}s "
                "before next sweep")
            # Try to refresh the driver too — connection may be stale.
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                driver = (driver_factory or _default_driver)()
            except Exception as exc:  # noqa: BLE001
                log(f"driver re-open failed: {exc!r}")
        else:
            backoff_seconds = 0

        if once:
            break
        # Sleep until the soonest due, capped at 5s for responsiveness
        # — unless we're in Neo4j backoff, then sleep the backoff window.
        if backoff_seconds:
            end = time.time() + backoff_seconds
        else:
            wait = max(1.0, min(due.values(), default=now + 60) - time.time())
            wait = min(wait, 5.0)
            end = time.time() + wait
        # Cooperative sleep that wakes on signal.
        while not flag.stop and time.time() < end:
            time.sleep(0.5)

    _log_to(log_path, "scheduler shutting down")
    try:
        driver.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Daemonize (POSIX double-fork) ────────────────────────────────────

def daemonize(*, log_path: Path | None = None) -> int:
    """POSIX double-fork. Parent returns child PID. Child runs run_loop."""
    if PID_PATH.is_file():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)
            raise RuntimeError(f"scheduler already running, pid={pid}")
        except (ValueError, ProcessLookupError):
            pass

    pid = os.fork()
    if pid > 0:
        # First parent — wait for first child to fork its own and exit.
        os.waitpid(pid, 0)
        # Read pid the grandchild wrote.
        for _ in range(50):
            if PID_PATH.exists():
                try:
                    return int(PID_PATH.read_text().strip())
                except ValueError:
                    pass
            time.sleep(0.05)
        return -1

    # First child
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)

    # Grandchild — actual daemon
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))
    os.chdir("/")
    # Detach stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)

    try:
        run_loop(log_path=log_path)
    finally:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        os._exit(0)


def stop_daemon(*, wait_seconds: float = 30.0) -> bool:
    """Send SIGTERM and wait up to wait_seconds for actual exit.

    The previous implementation returned True the moment the signal was
    sent, leaving callers (like a redeploy script) to race with the PID
    file. Now we poll until the process is gone, then unlink the PID
    file so a subsequent ``daemonize()`` does not raise
    ``scheduler already running``.
    """
    if not PID_PATH.is_file():
        return False
    try:
        pid = int(PID_PATH.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        return False

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)        # signal 0 = liveness probe
            time.sleep(0.5)
        except ProcessLookupError:
            break

    # SIGKILL fallback if it ignored SIGTERM.
    try:
        os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
    except ProcessLookupError:
        pass

    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return True


def daemon_status() -> dict:
    out: dict = {"pid_file": str(PID_PATH), "running": False, "pid": None}
    if PID_PATH.is_file():
        try:
            pid = int(PID_PATH.read_text().strip())
            os.kill(pid, 0)
            out["running"] = True
            out["pid"] = pid
        except (ValueError, ProcessLookupError):
            pass
    out["repos"] = {n: asdict(s) for n, s in _read_status().items()}
    return out


# ─── Defaults ─────────────────────────────────────────────────────────

def _default_driver():
    from neo4j import GraphDatabase
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    return GraphDatabase.driver(uri, auth=(user, pw))


def _default_state():
    from aiforge_memory.store import state_db as sdb
    conn = sdb.open_db()
    sdb.migrate(conn)
    return conn


def _log_to(path: Path | None, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    target = Path(path or LOG_PATH)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as f:
            f.write(line)
    except OSError:
        pass
