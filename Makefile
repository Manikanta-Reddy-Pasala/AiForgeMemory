.PHONY: help install test test-L1 test-L2 test-L3 test-L4 test-L5 test-L6 test-L7 doctor lint

help:
	@echo "AiForgeMemory dev targets:"
	@echo "  install     uv venv + uv pip install -e .[dev]"
	@echo "  test        full pytest"
	@echo "  test-L<N>   per-layer gate (L1..L7)"
	@echo "  doctor      check repomix + neo4j + llm sidecars"
	@echo "  lint        ruff check"

install:
	uv venv .venv
	uv pip install -e ".[dev]"

test:
	.venv/bin/pytest aiforge_memory/tests -v

test-L1:
	.venv/bin/pytest aiforge_memory/tests/L1_repo_node/ -v

test-L2:
	.venv/bin/pytest aiforge_memory/tests/L2_service_extract/ -v

test-L3:
	.venv/bin/pytest aiforge_memory/tests/L3_file_summary/ -v

test-L4:
	.venv/bin/pytest aiforge_memory/tests/L4_symbols/ -v

test-L5:
	.venv/bin/pytest aiforge_memory/tests/L5_chunks_vectors/ -v

test-L6:
	.venv/bin/pytest aiforge_memory/tests/L6_translator/ -v

test-L7:
	.venv/bin/pytest aiforge_memory/tests/L7_bundle/ -v

doctor:
	.venv/bin/aiforge-memory doctor

lint:
	.venv/bin/ruff check aiforge_memory
