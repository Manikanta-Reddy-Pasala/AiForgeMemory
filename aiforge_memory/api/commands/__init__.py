"""Per-command modules for the ``aiforge-memory`` CLI.

Each subcommand exports a ``run(args)`` and a ``register(sub_parsers)``
hook; ``cli.py`` calls every module's ``register`` to compose the full
argparse tree. Adding a new command = drop a file + one line in
:data:`COMMAND_MODULES` below.
"""
from __future__ import annotations

from . import (
    doctor,
    forget,
    health,
    ingest,
    install_hook,
    link,
    list_memory,
    ops,
    recall,
    remember,
    schedule,
    services,
    stats,
    summarise_symbols,
    ui,
)
from . import (
    eval as eval_cmd,
)

# Order = order they appear in `aiforge-memory --help`. Keep grouped:
# core ingest/inspect first, then memory CRUD, then cross-repo, then ops.
COMMAND_MODULES = (
    ingest,
    stats,
    summarise_symbols,
    services,
    doctor,
    remember,
    recall,
    forget,
    list_memory,
    link,
    eval_cmd,
    install_hook,
    schedule,
    health,
    ops,
    ui,
)


__all__ = ["COMMAND_MODULES"]
