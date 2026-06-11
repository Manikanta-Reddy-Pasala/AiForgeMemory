"""N3 — batched embedding: one POST /embed_batch per 32 chunks instead
of one POST /embed per chunk, with per-item fallback on batch failure."""
from __future__ import annotations

from unittest.mock import patch

import httpx

from aiforge_memory.features.chunk import embed as em


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


def test_embed_batch_single_post_per_batch():
    texts = [f"t{i}" for i in range(5)]
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append((url, json))
        return _FakeResponse({"embeddings": [[0.1, 0.2]] * len(json["texts"])})

    with patch.object(em.httpx, "post", side_effect=fake_post):
        vecs = em._embed_batch(texts)
    assert len(posts) == 1
    assert posts[0][0].endswith("/embed_batch")
    assert posts[0][1]["texts"] == texts
    assert vecs == [[0.1, 0.2]] * 5


def test_embed_batch_splits_at_batch_size():
    texts = [f"t{i}" for i in range(em.EMBED_BATCH_SIZE + 1)]
    posts = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json["texts"])
        return _FakeResponse({"embeddings": [[0.5]] * len(json["texts"])})

    with patch.object(em.httpx, "post", side_effect=fake_post):
        vecs = em._embed_batch(texts)
    assert len(posts) == 2
    assert len(posts[0]) == em.EMBED_BATCH_SIZE
    assert len(posts[1]) == 1
    assert len(vecs) == len(texts)


def test_embed_batch_falls_back_per_item_on_batch_failure():
    texts = ["a", "b"]
    with patch.object(em.httpx, "post",
                      side_effect=httpx.ConnectError("down")), \
         patch.object(em, "_embed", return_value=[0.9]) as per_item:
        vecs = em._embed_batch(texts)
    assert vecs == [[0.9], [0.9]]
    assert per_item.call_count == 2


def test_embed_batch_length_mismatch_triggers_fallback():
    def short_response(url, json=None, timeout=None):
        return _FakeResponse({"embeddings": [[0.1]]})   # 1 vec for 2 texts

    with patch.object(em.httpx, "post", side_effect=short_response), \
         patch.object(em, "_embed", return_value=[0.7]) as per_item:
        vecs = em._embed_batch(["a", "b"])
    assert vecs == [[0.7], [0.7]]
    assert per_item.call_count == 2


def test_chunk_and_embed_uses_batch_endpoint(tmp_path):
    (tmp_path / "x.py").write_text("def f(): pass\n" * 5)
    from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol
    walked = [WalkedFile(
        repo="t", path="x.py", hash="h", lang="python", lines=5,
        symbols=[WalkedSymbol(fqname="x.py::f", kind="function",
                              file_path="x.py", signature="def f(): ...")],
    )]
    with patch.object(em, "_embed_batch",
                      side_effect=lambda ts: [[0.3]] * len(ts)) as batch:
        chunks, failed = em.chunk_and_embed(walked, repo="t",
                                            repo_root=tmp_path)
    assert batch.called
    assert failed == []
    assert all(c.embed_vec == [0.3] for c in chunks)
