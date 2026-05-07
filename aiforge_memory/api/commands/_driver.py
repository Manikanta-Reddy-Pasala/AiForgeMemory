"""Shared Neo4j driver helper.

Every command that talks to Neo4j goes through here so a connection
config change is a one-file edit.
"""
from __future__ import annotations

import os


def driver():
    """Open the project's Neo4j driver. Errors propagate to caller."""
    from neo4j import GraphDatabase

    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get("AIFORGE_NEO4J_PASSWORD", "password")
    return GraphDatabase.driver(uri, auth=(user, pw))


__all__ = ["driver"]
