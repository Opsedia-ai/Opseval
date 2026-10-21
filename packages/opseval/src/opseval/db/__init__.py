# SPDX-License-Identifier: Apache-2.0
"""Server mode on Postgres, the Langfuse model (ADR-0047).

Optional: it needs the ``server`` extra (``pip install 'opseval[server]'``). Local mode, the
CLI on catalogue folders and run folders, never imports it; an import-linter contract keeps
the run path free of it.
"""
