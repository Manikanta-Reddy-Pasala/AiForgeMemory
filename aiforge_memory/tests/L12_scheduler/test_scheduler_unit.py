"""L12 — scheduler config + lockfile + git poll-decide (no daemon spawn)."""
from __future__ import annotations

import os
import subprocess

from aiforge_memory.ingest import scheduler

# ─── Config persistence ───────────────────────────────────────────────

def test_config_round_trip(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    rs = scheduler.RepoSchedule(
        name="r1", path="/tmp/r1", interval_seconds=300,
    )
    cfg = scheduler.SchedulerConfig(repos=[rs])
    cfg.save(cfg_path)
    loaded = scheduler.SchedulerConfig.load(cfg_path)
    assert len(loaded.repos) == 1
    assert loaded.repos[0].name == "r1"
    assert loaded.repos[0].interval_seconds == 300


def test_add_repo_dedupes_by_name(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    scheduler.add_repo(
        scheduler.RepoSchedule(name="r1", path="/a", interval_seconds=60),
        path=cfg_path,
    )
    scheduler.add_repo(
        scheduler.RepoSchedule(name="r1", path="/b", interval_seconds=120),
        path=cfg_path,
    )
    cfg = scheduler.SchedulerConfig.load(cfg_path)
    assert len(cfg.repos) == 1
    assert cfg.repos[0].path == "/b"
    assert cfg.repos[0].interval_seconds == 120


def test_remove_repo(tmp_path):
    cfg_path = tmp_path / "scheduler.yaml"
    scheduler.add_repo(
        scheduler.RepoSchedule(name="r1", path="/a"),
        path=cfg_path,
    )
    assert scheduler.remove_repo("r1", path=cfg_path) is True
    assert scheduler.remove_repo("r1", path=cfg_path) is False


def test_load_missing_file_returns_empty(tmp_path):
    cfg = scheduler.SchedulerConfig.load(tmp_path / "missing.yaml")
    assert cfg.repos == []


def test_load_malformed_yaml_returns_empty(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("not: : yaml :")
    cfg = scheduler.SchedulerConfig.load(p)
    assert cfg.repos == []


# ─── Lockfile ─────────────────────────────────────────────────────────

def test_lock_acquire_release(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path)))
    assert scheduler._acquire_lock("repo1") is True
    assert scheduler._acquire_lock("repo1") is False  # held by us
    scheduler._release_lock("repo1")
    assert scheduler._acquire_lock("repo1") is True


def test_lock_reclaims_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(tmp_path)))
    lf = scheduler._lockfile("repoX")
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("99999999")           # pid that doesn't exist
    assert scheduler._acquire_lock("repoX") is True
    scheduler._release_lock("repoX")


# ─── git poll decision ────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_fetch_and_pull_skipped_when_dirty(tmp_path, monkeypatch):
    seq = [
        _FakeProc(returncode=0),       # fetch
        _FakeProc(stdout="3\n"),       # behind=3
        _FakeProc(stdout=" M file"),   # dirty
    ]
    def fake_run(*a, **kw):
        return seq.pop(0)
    monkeypatch.setattr(subprocess, "run", fake_run)

    out = scheduler.fetch_and_maybe_pull(tmp_path, do_pull=True)
    assert out.fetched is True
    assert out.behind == 3
    assert out.pulled is False
    assert out.skipped_reason == "dirty"


def test_fetch_and_pull_proceeds_when_clean(tmp_path, monkeypatch):
    seq = [
        _FakeProc(returncode=0),       # fetch
        _FakeProc(stdout="2\n"),       # behind
        _FakeProc(stdout=""),          # clean
        _FakeProc(stdout="origin/main\n"),  # has upstream
        _FakeProc(returncode=0),       # pull --ff-only
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: seq.pop(0))

    out = scheduler.fetch_and_maybe_pull(tmp_path, do_pull=True)
    assert out.fetched and out.pulled
    assert out.behind == 2


def test_fetch_only_when_up_to_date(tmp_path, monkeypatch):
    seq = [
        _FakeProc(returncode=0),       # fetch
        _FakeProc(stdout="0\n"),       # behind=0
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: seq.pop(0))
    out = scheduler.fetch_and_maybe_pull(tmp_path)
    assert out.fetched is True
    assert out.pulled is False
    assert out.behind == 0


def test_fetch_failure_returns_clean_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _FakeProc(returncode=1),
    )
    out = scheduler.fetch_and_maybe_pull(tmp_path)
    assert out.fetched is False and out.pulled is False
