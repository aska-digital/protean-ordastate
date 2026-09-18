#!/usr/bin/env python3
"""Ingestion helpers — thin re-export; main logic lives in orda2_cli.ingest."""
# This module exists to satisfy the §11 layout; ingest is implemented in orda2_cli.
from orda2.orda2_cli import cmd_ingest  # noqa: F401
