"""Chunk-test isolation: point the embed sidecar at a dead port so unit
tests never accidentally talk to a real sidecar running on :8764 —
they drive behaviour by patching ``_embed`` / ``_embed_batch``.

Opt-in (``pytest.mark.usefixtures``) rather than autouse because the
embed-config tests assert the real env-resolution behaviour."""
from __future__ import annotations

import pytest


@pytest.fixture()
def no_live_sidecar(monkeypatch):
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://127.0.0.1:9")
