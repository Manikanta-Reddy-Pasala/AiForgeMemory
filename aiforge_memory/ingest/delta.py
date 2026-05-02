"""Git-aware delta ingest.

Goal: re-index only the files that changed since the previous ingest.
Avoids the 69-min file-summary stage on a one-line edit.

Two change-detection strategies, in priority order:

    1. git diff       — fastest. Uses (prev_head_sha, current HEAD) from
                        state_db.git_state. Requires a git checkout.
    2. merkle hashes  — fallback. Hashes every source file, compares to
                        state_db.merkle_files. Same cost as a normal
                        walk, but only re-summarises / re-embeds changed
                        files (saves the LLM + embed-sidecar passes).

Public surface:
    delta.changed_files(repo_path, *, repo, state_conn) -> ChangedSet
    delta.ingest_delta(*, repo_name, repo_path, driver, state_conn,
                        skip_summaries=False, skip_chunks=False)
        -> IngestResult

Out of scope: full repo summary regeneration (Stage 2). If the user
wants that, they run the regular (non-delta) ingest.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from aiforge_memory.ingest import (
    edges, embed, file_summary, treesitter_walk,
)
from aiforge_memory.ingest.flow import IngestResult
from aiforge_memory.store import (
    chunk_writer, file_summary_writer, state_db as sdb, symbol_writer,
)


@dataclass
class ChangedSet:
    method: str                                 # 'git' | 'merkle' | 'cold'
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    head_sha: str = ""
    branch: str = ""

    @property
    def to_index(self) -> set[str]:
        return set(self.added) | set(self.modified)


# ─── Detection ────────────────────────────────────────────────────────

def changed_files(
    repo_path: str | Path, *, repo: str, state_conn,
) -> ChangedSet:
    """Compute the set of files to re-index. Cold-start (no prior state)
    returns method='cold' and empty lists — caller should fall back to
    full ingest in that case."""
    repo_path = Path(repo_path).resolve()

    head = _git_head(repo_path)
    prev = sdb.get_repo_git_head(state_conn, repo=repo)

    if head and prev and prev[0] != head[0]:
        added, mod, deleted = _git_diff(repo_path, prev[0], head[0])
        return ChangedSet(
            method="git", added=added, modified=mod, deleted=deleted,
            head_sha=head[0], branch=head[1],
        )

    # Fallback: walk + hash, compare to merkle_files
    prev_hashes = sdb.get_file_hashes(state_conn, repo=repo)
    if not prev_hashes:
        return ChangedSet(
            method="cold",
            head_sha=head[0] if head else "",
            branch=head[1] if head else "",
        )

    cur_hashes = _hash_repo(repo_path)
    added, modified, deleted = _diff_hashes(prev_hashes, cur_hashes)
    return ChangedSet(
        method="merkle",
        added=added, modified=modified, deleted=deleted,
        head_sha=head[0] if head else "",
        branch=head[1] if head else "",
    )


def _git_head(repo_path: Path) -> tuple[str, str] | None:
    """Return (sha, branch) from git, or None if not a git repo."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=5,
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_path), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if sha.returncode != 0 or branch.returncode != 0:
        return None
    return sha.stdout.strip(), branch.stdout.strip()


def _git_diff(
    repo_path: Path, base: str, head: str,
) -> tuple[list[str], list[str], list[str]]:
    """Return (added, modified, deleted) repo-relative paths between
    two SHAs."""
    try:
        r = subprocess.run(
            ["git", "diff", "--name-status", "-z", f"{base}..{head}"],
            cwd=str(repo_path), capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return [], [], []
    if r.returncode != 0:
        return [], [], []
    raw = r.stdout.decode("utf-8", "replace") if r.stdout else ""
    tokens = [t for t in raw.split("\0") if t]
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        i += 1
        if status.startswith("R") or status.startswith("C"):
            # Rename / copy: status, old, new
            if i + 1 < len(tokens):
                old, new = tokens[i], tokens[i + 1]
                deleted.append(old)
                added.append(new)
                i += 2
            continue
        if i >= len(tokens):
            break
        path = tokens[i]
        i += 1
        if status == "A":
            added.append(path)
        elif status == "M":
            modified.append(path)
        elif status == "D":
            deleted.append(path)
        elif status == "T":  # type change (symlink, etc.)
            modified.append(path)
    return added, modified, deleted


def _hash_repo(repo_path: Path) -> dict[str, str]:
    """Walk + hash every source-ish file; honor _SKIP_DIRS."""
    out: dict[str, str] = {}
    skip = treesitter_walk._SKIP_DIRS
    for path in repo_path.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_path)
        parts = set(rel.parts)
        if parts & skip:
            continue
        # only hash files we'd actually index
        lang = treesitter_walk.lang_for(rel)
        if lang is None:
            continue
        try:
            data = path.read_bytes()
        except (OSError, ValueError):
            continue
        out[str(rel)] = hashlib.sha256(data).hexdigest()
    return out


def _diff_hashes(
    prev: dict[str, str], cur: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    added = sorted(set(cur) - set(prev))
    deleted = sorted(set(prev) - set(cur))
    modified = sorted(p for p in (set(cur) & set(prev)) if cur[p] != prev[p])
    return added, modified, deleted


# ─── Apply ────────────────────────────────────────────────────────────

def ingest_delta(
    *,
    repo_name: str,
    repo_path: str | Path,
    driver,
    state_conn,
    skip_summaries: bool = False,
    skip_chunks: bool = False,
) -> IngestResult:
    """Re-index only changed files. Returns IngestResult with status
    'delta_applied' | 'no_changes' | 'cold_start_required'."""
    repo_path = Path(repo_path).resolve()
    cs = changed_files(repo_path, repo=repo_name, state_conn=state_conn)

    if cs.method == "cold":
        return IngestResult(
            status="cold_start_required", pack_sha="", repo=repo_name,
        )
    if not cs.to_index and not cs.deleted:
        # still update head pointer so we don't recompute next time
        if cs.head_sha:
            sdb.set_repo_git_head(
                state_conn, repo=repo_name,
                head_sha=cs.head_sha, branch=cs.branch,
            )
        return IngestResult(
            status="no_changes", pack_sha="", repo=repo_name,
        )

    # Delete graph entries for removed files
    if cs.deleted:
        _detach_files(driver, repo=repo_name, paths=cs.deleted)

    # Walk only changed files
    walked = _walk_subset(
        repo_path, repo=repo_name, paths=sorted(cs.to_index),
    )

    files_count = symbols_count = imports_count = calls_count = 0
    summaries_updated = chunks_count = 0

    if walked:
        scounts = symbol_writer.upsert_files_and_symbols(
            driver, repo=repo_name, walked_files=walked,
        )
        files_count = scounts["files"]
        symbols_count = scounts["symbols"]
        imports_count = scounts["imports"]

        call_edges = edges.resolve_calls_with_source(
            walked, repo=repo_name, repo_root=repo_path,
        )
        ccounts = symbol_writer.upsert_call_edges(
            driver, repo=repo_name, edges=call_edges,
            file_paths=[wf.path for wf in walked],
        )
        calls_count = ccounts["calls"]

        if not skip_summaries:
            summaries = file_summary.summarize_files(
                walked, repo=repo_name, repo_root=repo_path,
            )
            sumcounts = file_summary_writer.write_summaries(
                driver, repo=repo_name, summaries=summaries,
            )
            summaries_updated = sumcounts["updated"]

        if not skip_chunks:
            chunks = embed.chunk_and_embed(
                walked, repo=repo_name, repo_root=repo_path,
            )
            if chunks:
                ccounts = chunk_writer.upsert_chunks(
                    driver, repo=repo_name, chunks=chunks,
                )
                chunks_count = ccounts["chunks"]

    # Update merkle + git head so next delta is fresh.
    if walked:
        sdb.upsert_file_hashes(
            state_conn, repo=repo_name,
            hashes={wf.path: wf.hash for wf in walked},
        )
    if cs.head_sha:
        sdb.set_repo_git_head(
            state_conn, repo=repo_name,
            head_sha=cs.head_sha, branch=cs.branch,
        )

    return IngestResult(
        status="delta_applied",
        pack_sha=cs.head_sha or "",
        repo=repo_name,
        files_count=files_count,
        symbols_count=symbols_count,
        imports_count=imports_count,
        calls_count=calls_count,
        summaries_updated=summaries_updated,
        chunks_count=chunks_count,
    )


def _walk_subset(
    repo_path: Path, *, repo: str, paths: list[str],
) -> list[treesitter_walk.WalkedFile]:
    """Run the same per-file logic as walk_repo, but only for `paths`."""
    walked = treesitter_walk.walk_repo(repo_path, repo=repo)
    target = set(paths)
    return [wf for wf in walked if wf.path in target]


_DETACH_FILE_CY = """
MATCH (f:File_v2 {repo:$repo, path:$path})
DETACH DELETE f
"""

_DETACH_FILE_SYMBOLS_CY = """
MATCH (f:File_v2 {repo:$repo, path:$path})-[:DEFINES]->(s:Symbol_v2)
DETACH DELETE s
"""

_DETACH_FILE_CHUNKS_CY = """
MATCH (f:File_v2 {repo:$repo, path:$path})-[:CHUNKED_AS]->(c:Chunk_v2)
DETACH DELETE c
"""


def _detach_files(driver, *, repo: str, paths: list[str]) -> None:
    """Remove File_v2 + descendant Symbol_v2 / Chunk_v2 for deleted paths."""
    with driver.session() as s:
        for p in paths:
            s.run(_DETACH_FILE_SYMBOLS_CY, repo=repo, path=p).consume()
            s.run(_DETACH_FILE_CHUNKS_CY, repo=repo, path=p).consume()
            s.run(_DETACH_FILE_CY, repo=repo, path=p).consume()


# ─── Hook installer ───────────────────────────────────────────────────

_POST_COMMIT_TEMPLATE = """#!/bin/sh
# AiForgeMemory delta-ingest hook (auto-installed)
# Re-runs incremental ingest after each commit. Safe to remove.
exec aiforge-memory ingest "{repo_name}" --path "{repo_path}" --delta >/dev/null 2>&1 &
"""


def install_post_commit_hook(repo_path: str | Path, repo_name: str) -> Path:
    """Write `.git/hooks/post-commit` for delta ingest. Returns the path
    written. Raises FileNotFoundError if .git/hooks doesn't exist."""
    repo_path = Path(repo_path).resolve()
    hook_dir = repo_path / ".git" / "hooks"
    if not hook_dir.is_dir():
        raise FileNotFoundError(f"not a git checkout: {repo_path}")
    hook = hook_dir / "post-commit"
    hook.write_text(_POST_COMMIT_TEMPLATE.format(
        repo_name=repo_name, repo_path=str(repo_path),
    ))
    hook.chmod(0o755)
    return hook
