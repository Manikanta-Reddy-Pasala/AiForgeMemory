"""M7 — embed_config() is env-pluggable, defaults reproduce bge-m3/1024."""
from __future__ import annotations

from unittest.mock import patch

from aiforge_memory.features.chunk import embed as em


def test_defaults_reproduce_bge_m3(monkeypatch) -> None:
    for k in ("AIFORGE_EMBED_MODEL", "AIFORGE_EMBED_URL", "AIFORGE_EMBED_DIM"):
        monkeypatch.delenv(k, raising=False)
    cfg = em.embed_config()
    assert cfg["model"] == "bge-m3"
    assert cfg["url"] == "http://127.0.0.1:8764"
    assert cfg["dim"] == 1024


def test_env_overrides_take_effect(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_EMBED_MODEL", "nomic-embed-text")
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://10.0.0.5:9000/")
    monkeypatch.setenv("AIFORGE_EMBED_DIM", "768")
    cfg = em.embed_config()
    assert cfg["model"] == "nomic-embed-text"
    assert cfg["url"] == "http://10.0.0.5:9000/"
    assert cfg["dim"] == 768


def test_config_keys_present(monkeypatch) -> None:
    cfg = em.embed_config()
    assert set(cfg) == {"model", "url", "dim"}


def test_embed_posts_to_configured_url(monkeypatch) -> None:
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://example.test:1234")
    monkeypatch.setenv("AIFORGE_EMBED_MODEL", "custom-model")
    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"embedding": [0.1, 0.2, 0.3]}

    def fake_post(url, json, timeout):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    with patch.object(em.httpx, "post", fake_post):
        vec = em._embed("hello")
    assert vec == [0.1, 0.2, 0.3]
    assert captured["url"] == "http://example.test:1234/embed"
    # model is forwarded so a multi-model sidecar can route
    assert captured["json"].get("model") == "custom-model"


def test_default_embed_url_constant_unchanged() -> None:
    # backward-compat: legacy callers reading the module constant still work
    assert em.DEFAULT_EMBED_URL == em.embed_config()["url"]
