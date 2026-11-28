import sqlite3
from unittest.mock import Mock
import pytest
from acd.database.retry import setup_pragma


def test_transient_lock_is_retried_without_deleting_database():
    connection = Mock()
    connection.execute.side_effect = [sqlite3.OperationalError('database is locked'), 'ok']
    assert setup_pragma(connection, 'PRAGMA journal_mode=WAL', delay=0) == 'ok'
    assert connection.execute.call_count == 2


def test_persistent_lock_is_bounded():
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError('database is locked')
    with pytest.raises(sqlite3.OperationalError):
        setup_pragma(connection, 'PRAGMA journal_mode=WAL', attempts=3, delay=0)
    assert connection.execute.call_count == 3


def test_non_lock_errors_are_never_retried():
    connection = Mock()
    connection.execute.side_effect = sqlite3.OperationalError('disk I/O error')
    with pytest.raises(sqlite3.OperationalError):
        setup_pragma(connection, 'PRAGMA journal_mode=WAL', delay=0)
    assert connection.execute.call_count == 1
