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


@pytest.fixture
def settings(tmp_path):
    from dash_helpers import make_settings

    return make_settings(tmp_path / "data")


@pytest.fixture
def app(settings):
    from coral_dashboard.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture
def app_store(app, settings):
    """A Store on the same database the app uses."""
    from coral_dashboard.db import connect

    connection = connect(settings.db_path)
    yield Store(connection, app.state.coral.ctx)
    connection.close()
