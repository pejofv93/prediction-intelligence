"""Fixtures de tests de scripts/migration/pg_store.py. Necesitan un Postgres de usar y tirar en PG_DSN
(el workflow pg-store-test.yml levanta uno como service container). Sin PG_DSN se saltan,
salvo los tests puros (test_pure.py)."""
import os
import sys
import uuid

import pytest

MIGRATION = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "migration"))
if MIGRATION not in sys.path:
    sys.path.insert(0, MIGRATION)

import pg_store  # noqa: E402


@pytest.fixture(scope="session")
def client():
    dsn = os.environ.get("PG_DSN")
    if not dsn:
        pytest.skip("PG_DSN no definido: tests de Postgres omitidos")
    os.environ.setdefault("PG_POOL_MAX", "25")
    pool = pg_store.get_pool(dsn)
    yield pg_store.PgClient(pool=pool)
    pg_store.close_pool()


@pytest.fixture
def coll(client):
    """Colección nueva y vacía para cada test."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    yield client.collection(name)
    with client.pool.connection() as conn:
        conn.execute("DELETE FROM fs_docs WHERE collection=%s", (name,))
