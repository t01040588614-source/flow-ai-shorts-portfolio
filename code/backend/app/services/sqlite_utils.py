"""SQLite connections that close when their `with` block ends.

A plain sqlite3.Connection used as `with sqlite3.connect(path) as conn:` commits (or rolls back)
on exit but stays open until garbage collection. On Windows the file stays locked meanwhile,
and the test suite reported ~14,700 unclosed connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType
from typing import Any, Literal


class ClosingConnection(sqlite3.Connection):
    """`with` commits or rolls back as usual, then closes the connection."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path: str | Path, **kwargs: Any) -> sqlite3.Connection:
    """sqlite3.connect for one `with` block: closed when the block ends."""
    return sqlite3.connect(path, factory=ClosingConnection, **kwargs)
