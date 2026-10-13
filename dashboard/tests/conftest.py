from __future__ import annotations

import pytest
from coral_dashboard.db import connect, migrate
from coral_dashboard.repo import Store
from dash_helpers import CTX


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "test.sqlite3")
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def store(conn):
    return Store(conn, CTX)
