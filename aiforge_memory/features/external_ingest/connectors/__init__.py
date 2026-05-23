"""External-source connectors built on the gap-9 ingest spine.

Each connector:
1. Fetches raw text from the source (Linear / Jira / Slack / Notion).
2. Calls :func:`ingest_external_source` with sensible defaults.

KISS: minimal HTTP, no SDK. Auth via env vars only. Soft-fail.
"""
from __future__ import annotations

from .jira import ingest_jira_issue
from .linear import ingest_linear_issue

__all__ = ["ingest_jira_issue", "ingest_linear_issue"]
