"""
Shared pytest fixtures.

Every test gets its own freshly seeded database in a temporary folder, so
tests never interfere with each other or with the demo database in
data/sophia.db.
"""

import sys
from pathlib import Path

import pytest

# Make the package importable without installing it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sophia import db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    """A fresh, fully seeded database for one test."""
    connection = db.reset_database(tmp_path / "test_sophia.db")
    yield connection
    connection.close()


@pytest.fixture
def empty_conn(tmp_path):
    """An empty database with the schema but no seed data."""
    connection = db.connect(tmp_path / "empty_sophia.db")
    db.create_schema(connection)
    yield connection
    connection.close()
