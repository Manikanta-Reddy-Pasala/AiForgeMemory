"""N2 — a partial embed failure must not lose chunks permanently.

Before: first failed chunk → break, the file's remaining chunks were
dropped, _PRUNE_FILE_CHUNKS deleted the graph copy and the merkle hash
advanced anyway — the file was never retried.

Now: chunk_and_embed returns (chunks, failed_paths); failed files emit
zero chunks (no upsert, no prune) and delta skips their merkle-hash
update so the next sweep retries them.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aiforge_memory.core import state as sdb
from aiforge_memory.features.chunk import embed as em
from aiforge_memory.features.delta import extract as delta
from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol


def _walked(path: str, lang: str = "python") -> WalkedFile:
    return WalkedFile(
        repo="t", path=path, hash=f"hash-{path}", lang=lang, lines=10,
        symbols=[WalkedSymbol(fqname=f"{path}::demo", kind="function",
                              file_path=path, signature="def demo(): ...")],
    )


def test_failed_file_does_not_poison_other_files(tmp_path) -> None:
    (tmp_path / "good.py").write_text("def f(): pass\n" * 5)
    (tmp_path / "bad.py").write_text("def g(): pass\n" * 5)
    walked = [_walked("bad.py"), _walked("good.py")]

    def fake_embed(text):
        if "g()" in text:
            raise RuntimeError("sidecar 500")
        return [0.1] * 1024

    with patch.object(em, "_embed", side_effect=fake_embed):
        chunks, failed = em.chunk_and_embed(walked, repo="t",
                                            repo_root=tmp_path)
    assert failed == ["bad.py"]
    assert {c.file_path for c in chunks} == {"good.py"}


def test_mid_file_failure_drops_all_partial_chunks(tmp_path) -> None:
    """A file whose 2nd chunk fails must emit no chunks at all —
    otherwise the prune pass would delete its other graph chunks."""
    (tmp_path / "x.py").write_text("\n".join(f"l{i}" for i in range(150)))
    walked = [_walked("x.py")]
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("sidecar down")
        return [0.1] * 1024

    with patch.object(em, "_embed", side_effect=flaky):
        chunks, failed = em.chunk_and_embed(walked, repo="t",
                                            repo_root=tmp_path)
    assert chunks == []
    assert failed == ["x.py"]


def test_delta_skips_merkle_update_for_failed_files(tmp_path) -> None:
    """ingest_delta must leave the failed file's hash stale so the next
    merkle diff sees it as modified and retries."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "a.py").write_text("def a(): pass\n")
    (repo_dir / "b.py").write_text("def b(): pass\n")

    state = sdb.open_db(tmp_path / "state.db")
    sdb.migrate(state)
    # Prior state: both files known with old hashes (forces merkle path).
    sdb.upsert_file_hashes(state, repo="t",
                           hashes={"a.py": "old-a", "b.py": "old-b"})

    driver = MagicMock()

    def fake_embed(text):
        if "def b" in text:
            raise RuntimeError("sidecar 500")
        return [0.1] * 1024

    with patch.object(em, "_embed", side_effect=fake_embed), \
         patch("aiforge_memory.features.delta.extract.file_summary"
               ".summarize_files", return_value=[]):
        res = delta.ingest_delta(
            repo_name="t", repo_path=repo_dir,
            driver=driver, state_conn=state,
        )
    assert res.status == "delta_applied"
    hashes = sdb.get_file_hashes(state, repo="t")
    assert hashes["a.py"] != "old-a"          # advanced
    assert hashes["b.py"] == "old-b"          # stale → retried next delta
