"""Bounded retry for transient SQLite setup locks only."""
import sqlite3
import time


def setup_pragma(connection, statement, attempts=5, delay=0.05):
    for attempt in range(attempts):
        try:
            return connection.execute(statement)
        except sqlite3.OperationalError as exc:
            code = getattr(exc, 'sqlite_errorcode', None)
            locked = (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) if code is not None else str(exc) in ('database is locked', 'database table is locked')
            if not locked or attempt == attempts - 1:
                raise
            time.sleep(delay)
