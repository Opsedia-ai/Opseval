# SPDX-License-Identifier: Apache-2.0
"""Dotted paths into JSON payloads, one implementation for every reader.

``data.refunds.0.id`` walks objects by key and lists by index. A missing step raises
``KeyError``: absent is different from present-and-null, and each caller decides what absent
means for it.
"""

from __future__ import annotations

from typing import Any


def dig(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


__all__ = ["dig"]
