"""Explicit snapshot scopes; Database.session remains an autocommit connection scope."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Concatenate, Protocol

import duckdb


def transaction_active(conn: duckdb.DuckDBPyConnection) -> bool:
    """Detect a caller-owned transaction without issuing a nested BEGIN.

    Autocommit statements receive distinct transaction IDs; an explicit
    transaction retains its ID. These probes read no application data.
    Connections must not be shared concurrently between threads.
    """
    first = conn.execute("SELECT current_transaction_id()").fetchone()
    second = conn.execute("SELECT current_transaction_id()").fetchone()
    return first == second


@contextmanager
def read_snapshot(conn: duckdb.DuckDBPyConnection) -> Iterator[None]:
    """Join an existing transaction, or own one coherent logical read.

    A caller may also materialize factor results in this scope. Repositories
    which own BEGIN/COMMIT (e.g. portfolio persistence) run after this scope.
    Never commit or roll back a transaction owned by our caller.
    """
    if transaction_active(conn):
        yield
        return
    conn.execute("BEGIN TRANSACTION")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException as exc:
        try:
            conn.execute("ROLLBACK")
        except Exception as rollback_error:
            exc.add_note(f"Snapshot rollback also failed: {rollback_error}")
        raise


class _ConnectionOwner(Protocol):
    _conn: duckdb.DuckDBPyConnection


def consistent_read[Owner: _ConnectionOwner, **P, Result](
    method: Callable[Concatenate[Owner, P], Result],
) -> Callable[Concatenate[Owner, P], Result]:
    """Keep nested service reads on the same MVCC snapshot."""

    @wraps(method)
    def wrapped(self: Owner, /, *args: P.args, **kwargs: P.kwargs) -> Result:
        with read_snapshot(self._conn):
            return method(self, *args, **kwargs)

    return wrapped
