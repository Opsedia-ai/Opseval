# SPDX-License-Identifier: Apache-2.0
"""HTTP API for the web console (``opseval serve``). Needs the ``serve`` extra."""

from opseval.server.app import create_app

__all__ = ["create_app"]
