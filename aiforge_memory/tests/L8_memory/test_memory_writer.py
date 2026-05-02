"""L8 — memory_writer Decision/Observation/Note/Doc + MENTIONS edges.

Live-Neo4j test. Cleanup at the end of the module.
"""
from __future__ import annotations

import os

import pytest

from aiforge_memory.ingest.repo_summary import RepoSummary
from aiforge_memory.store import memory_writer, repo_writer, schema

pytestmark = pytest.mark.live_neo4j


@pytest.fixture(scope="module")
def driver():
    try:
        from neo4j import GraphDatabase
    except ImportError:
        pytest.skip("neo4j driver not installed")
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    drv = GraphDatabase.driver(uri, auth=(user, pw))
    try:
        with drv.session() as s:
            s.run("RETURN 1").consume()
    except Exception as exc:
        pytest.skip(f"Neo4j unreachable: {exc}")
    schema.apply(drv)

    # Bootstrap a Repo node these memories can attach to.
    repo_writer.upsert_repo(
        drv, name="test_mem_repo",
        path="/tmp/test_mem_repo",
        summary=RepoSummary(lang_primary="python", runbook_md="r" * 200),
        pack_sha="sha",
    )
    # Plus a File_v2 + Symbol_v2 to test MENTIONS edges.
    with drv.session() as s:
        s.run(
            "MERGE (f:File_v2 {repo:'test_mem_repo', path:'src/foo.py'}) "
            "ON CREATE SET f.schema_version='codemem-v1' "
            "MERGE (sy:Symbol_v2 {repo:'test_mem_repo', "
            "       fqname:'foo.bar.Baz::run'}) "
            "ON CREATE SET sy.schema_version='codemem-v1'"
        ).consume()

    yield drv

    with drv.session() as s:
        s.run(
            "MATCH (n) WHERE n.repo = 'test_mem_repo' DETACH DELETE n"
        ).consume()
        s.run("MATCH (r:Repo {name:'test_mem_repo'}) DETACH DELETE r").consume()
    drv.close()


def test_decision_creates_node_with_records_edge(driver) -> None:
    out = memory_writer.upsert_decision(
        driver, repo="test_mem_repo",
        title="Use NATS over Kafka",
        body="ADR-001",
        rationale="Lower ops overhead in single-cluster deployment.",
        author="manik", session_id="s1",
        tags=["arch", "messaging"],
        refs=["src/foo.py"],
    )
    assert out["label"] == "Decision_v2"
    nid = out["id"]
    with driver.session() as s:
        row = s.run(
            "MATCH (r:Repo {name:'test_mem_repo'})-[:RECORDS]->"
            "(d:Decision_v2 {id:$id})-[:MENTIONS]->(f:File_v2) "
            "RETURN d.title AS title, d.rationale AS r, f.path AS p",
            id=nid,
        ).single()
    assert row["title"] == "Use NATS over Kafka"
    assert "Lower ops" in row["r"]
    assert row["p"] == "src/foo.py"


def test_decision_supersedes_chains_status(driver) -> None:
    a = memory_writer.upsert_decision(
        driver, repo="test_mem_repo",
        title="Original", body="b1", status="active",
    )
    b = memory_writer.upsert_decision(
        driver, repo="test_mem_repo",
        title="Revised", body="b2",
        supersedes_id=a["id"],
    )
    with driver.session() as s:
        old = s.run(
            "MATCH (d:Decision_v2 {id:$id}) RETURN d.status AS st",
            id=a["id"],
        ).single()
        edge = s.run(
            "MATCH (n:Decision_v2 {id:$nid})-[:SUPERSEDES]->"
            "(o:Decision_v2 {id:$oid}) RETURN o.id AS oid",
            nid=b["id"], oid=a["id"],
        ).single()
    assert old["st"] == "superseded"
    assert edge is not None


def test_observation_links_symbol_via_mentions(driver) -> None:
    out = memory_writer.upsert_observation(
        driver, repo="test_mem_repo",
        kind="bug",
        text="Race condition under concurrent push",
        refs=["foo.bar.Baz::run"],
        embed_vec=None,
    )
    assert out["label"] == "Observation_v2"
    with driver.session() as s:
        row = s.run(
            "MATCH (o:Observation_v2 {id:$id})-[:MENTIONS]->(s:Symbol_v2) "
            "RETURN o.kind AS k, s.fqname AS fq",
            id=out["id"],
        ).single()
    assert row["k"] == "bug"
    assert row["fq"] == "foo.bar.Baz::run"


def test_observation_with_vector_index(driver) -> None:
    """Observation_v2 with embed_vec should be retrievable via the
    vector index. Uses a stub 1024d embedding."""
    vec = [0.0] * 1024
    vec[0] = 1.0
    out = memory_writer.upsert_observation(
        driver, repo="test_mem_repo", text="learned that JetStream needs ack",
        kind="learning", embed_vec=vec,
    )
    rows = memory_writer.recall_observations(
        driver, repo="test_mem_repo", query_vec=vec, k=5,
    )
    ids = [r["id"] for r in rows]
    assert out["id"] in ids


def test_note_basic(driver) -> None:
    out = memory_writer.upsert_note(
        driver, repo="test_mem_repo",
        title="Onboarding", body="set up venv first", tags=["howto"],
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (n:Note_v2 {id:$id}) RETURN n.title AS t, n.body AS b",
            id=out["id"],
        ).single()
    assert row["t"] == "Onboarding"
    assert "venv" in row["b"]


def test_doc_basic(driver) -> None:
    out = memory_writer.upsert_doc(
        driver, repo="test_mem_repo",
        title="NATS docs", body="JetStream consumers...",
        url="https://docs.nats.io/jetstream", source_kind="web",
    )
    with driver.session() as s:
        row = s.run(
            "MATCH (d:Doc_v2 {id:$id}) RETURN d.url AS u, d.source_kind AS k",
            id=out["id"],
        ).single()
    assert row["u"].startswith("https://docs.nats.io")
    assert row["k"] == "web"


def test_list_memory_returns_all_kinds(driver) -> None:
    rows = memory_writer.list_memory(driver, repo="test_mem_repo", limit=200)
    labels = {r["label"] for r in rows}
    assert "Decision_v2" in labels
    assert "Observation_v2" in labels
    assert "Note_v2" in labels


def test_forget_removes_node(driver) -> None:
    n = memory_writer.upsert_note(
        driver, repo="test_mem_repo", title="ephemeral", body="x",
    )
    res = memory_writer.forget(
        driver, repo="test_mem_repo", node_id=n["id"], label="Note_v2",
    )
    assert res["deleted"] == n["id"]
    with driver.session() as s:
        row = s.run(
            "MATCH (n:Note_v2 {id:$id}) RETURN n", id=n["id"],
        ).single()
    assert row is None


def test_forget_unknown_label_raises(driver) -> None:
    with pytest.raises(ValueError):
        memory_writer.forget(
            driver, repo="test_mem_repo", node_id="x", label="Bogus_v2",
        )
