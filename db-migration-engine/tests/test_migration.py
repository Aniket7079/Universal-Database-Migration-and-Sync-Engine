"""
Full test suite for DB Migration Engine.
Uses SQLite in-memory for fast isolated testing.
Run: pytest tests/ -v
"""
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.session import get_db, Base
from app.services.transformer import apply_row_transforms, _mask_email, _mask_phone, _cast_bool
from app.schemas.migration import ColumnMapping, TransformType

# ── Test database setup ───────────────────────────────────────────────────────
TEST_DB = "sqlite:///./test_migration.db"
test_engine = create_engine(TEST_DB, connect_args={"check_same_thread": False})
TestSession  = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_db


@pytest.fixture(autouse=True, scope="session")
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    import os
    if os.path.exists("test_migration.db"):
        os.remove("test_migration.db")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def source_conn(client):
    r = client.post("/api/v1/connections", json={
        "name"   : f"test-source-{uuid.uuid4().hex[:8]}",
        "db_type": "sqlite",
        "url"    : "sqlite:///./test_source.db",
    })
    assert r.status_code == 201
    yield r.json()
    client.delete(f"/api/v1/connections/{r.json()['id']}")


@pytest.fixture
def target_conn(client):
    r = client.post("/api/v1/connections", json={
        "name"   : f"test-target-{uuid.uuid4().hex[:8]}",
        "db_type": "sqlite",
        "url"    : "sqlite:///./test_target.db",
    })
    assert r.status_code == 201
    yield r.json()
    client.delete(f"/api/v1/connections/{r.json()['id']}")


# ── Unit tests: Transform functions ──────────────────────────────────────────

class TestTransforms:
    def test_mask_email_normal(self):
        assert _mask_email("alice@example.com") == "a****@example.com"

    def test_mask_email_short_local(self):
        assert _mask_email("a@example.com") == "a***@example.com"

    def test_mask_email_none(self):
        assert _mask_email(None) is None

    def test_mask_email_no_at(self):
        assert _mask_email("notanemail") == "notanemail"

    def test_mask_phone_10_digits(self):
        result = _mask_phone("9876543210")
        assert result.endswith("3210")
        assert result.startswith("******")

    def test_mask_phone_with_formatting(self):
        result = _mask_phone("+91-98765-43210")
        assert result.endswith("3210")

    def test_mask_phone_none(self):
        assert _mask_phone(None) is None

    def test_cast_bool_int_1(self):
        assert _cast_bool(1) is True

    def test_cast_bool_int_0(self):
        assert _cast_bool(0) is False

    def test_cast_bool_string_true(self):
        assert _cast_bool("true") is True
        assert _cast_bool("yes") is True
        assert _cast_bool("1") is True

    def test_cast_bool_string_false(self):
        assert _cast_bool("false") is False

    def test_cast_bool_none(self):
        assert _cast_bool(None) is None

    def test_mask_full(self):
        mappings = [ColumnMapping(source="ssn", target="ssn", transform=TransformType.mask_full)]
        row = {"ssn": "123-45-6789", "name": "Alice"}
        result = apply_row_transforms(row, mappings)
        assert result["ssn"] == "[REDACTED]"
        assert result["name"] == "Alice"

    def test_rename_column(self):
        mappings = [ColumnMapping(source="email_id", target="email", transform=TransformType.none)]
        row = {"email_id": "alice@test.com", "age": 25}
        result = apply_row_transforms(row, mappings)
        assert "email" in result
        assert "email_id" not in result
        assert result["age"] == 25

    def test_skip_column(self):
        mappings = [ColumnMapping(source="password", target="password", skip=True)]
        row = {"password": "secret123", "name": "Alice"}
        result = apply_row_transforms(row, mappings)
        assert "password" not in result
        assert result["name"] == "Alice"

    def test_no_mappings_passthrough(self):
        row = {"id": 1, "name": "Alice", "email": "alice@test.com"}
        result = apply_row_transforms(row, [])
        assert result == row

    def test_uppercase_transform(self):
        mappings = [ColumnMapping(source="country", target="country", transform=TransformType.uppercase)]
        row = {"country": "india"}
        result = apply_row_transforms(row, mappings)
        assert result["country"] == "INDIA"

    def test_cast_int_transform(self):
        mappings = [ColumnMapping(source="score", target="score", transform=TransformType.cast_int)]
        row = {"score": "42"}
        result = apply_row_transforms(row, mappings)
        assert result["score"] == 42

    def test_multiple_transforms_same_row(self):
        mappings = [
            ColumnMapping(source="email", target="email",   transform=TransformType.mask_email),
            ColumnMapping(source="phone", target="phone",   transform=TransformType.mask_phone),
            ColumnMapping(source="flag",  target="flag",    transform=TransformType.tinyint_to_bool),
            ColumnMapping(source="pwd",   target="pwd",     skip=True),
        ]
        row = {"email": "alice@test.com", "phone": "9876543210", "flag": 1, "pwd": "secret"}
        result = apply_row_transforms(row, mappings)
        assert result["email"].startswith("a")
        assert result["email"].endswith("@test.com")
        assert result["phone"].endswith("3210")
        assert result["flag"] is True
        assert "pwd" not in result


# ── API tests ─────────────────────────────────────────────────────────────────

class TestConnectionAPI:
    def test_create_connection(self, client):
        r = client.post("/api/v1/connections", json={
            "name"   : "my-source-db",
            "db_type": "mysql",
            "url"    : "mysql+pymysql://user:pass@localhost:3306/mydb",
        })
        assert r.status_code == 201
        assert r.json()["name"] == "my-source-db"
        assert r.json()["db_type"] == "mysql"

    def test_duplicate_connection_rejected(self, client):
        client.post("/api/v1/connections", json={
            "name": "dup-conn", "db_type": "sqlite", "url": "sqlite:///./dup.db"
        })
        r = client.post("/api/v1/connections", json={
            "name": "dup-conn", "db_type": "sqlite", "url": "sqlite:///./dup.db"
        })
        assert r.status_code == 409

    def test_list_connections(self, client):
        r = client.get("/api/v1/connections")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_invalid_url_for_postgres(self, client):
        r = client.post("/api/v1/connections", json={
            "name"   : "bad-pg",
            "db_type": "postgresql",
            "url"    : "mysql+pymysql://wrong-url",  # wrong URL for pg type
        })
        assert r.status_code == 422

    def test_delete_connection(self, client):
        r = client.post("/api/v1/connections", json={
            "name": "to-del", "db_type": "sqlite", "url": "sqlite:///./del.db"
        })
        cid = r.json()["id"]
        r = client.delete(f"/api/v1/connections/{cid}")
        assert r.status_code == 204


class TestMigrationConfigAPI:
    def test_create_config(self, client, source_conn, target_conn):
        r = client.post("/api/v1/configs", json={
            "name"            : f"test-config-{uuid.uuid4().hex[:8]}",
            "source_conn_id"  : source_conn["id"],
            "target_conn_id"  : target_conn["id"],
            "conflict_strategy": "skip",
            "batch_size"      : 500,
            "table_mappings"  : [{
                "source_table": "users",
                "target_table": "members",
                "columns"     : [
                    {"source": "email", "target": "email", "transform": "mask_email"},
                    {"source": "phone", "target": "phone", "transform": "mask_phone"},
                ]
            }]
        })
        assert r.status_code == 201
        data = r.json()
        assert data["name"].startswith("test-config-")
        assert data["batch_size"] == 500

    def test_same_source_target_rejected(self, client, source_conn):
        r = client.post("/api/v1/configs", json={
            "name"          : f"same-conn-{uuid.uuid4().hex[:8]}",
            "source_conn_id": source_conn["id"],
            "target_conn_id": source_conn["id"],   # same connection!
            "table_mappings": [{"source_table": "t", "target_table": "t"}]
        })
        assert r.status_code == 422

    def test_empty_table_mappings_rejected(self, client, source_conn, target_conn):
        r = client.post("/api/v1/configs", json={
            "name"          : f"empty-map-{uuid.uuid4().hex[:8]}",
            "source_conn_id": source_conn["id"],
            "target_conn_id": target_conn["id"],
            "table_mappings": []   # must have at least 1
        })
        assert r.status_code == 422

    def test_list_configs(self, client):
        r = client.get("/api/v1/configs")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_get_config_not_found(self, client):
        r = client.get("/api/v1/configs/nonexistent")
        assert r.status_code == 404

    def test_delete_config(self, client, source_conn, target_conn):
        r = client.post("/api/v1/configs", json={
            "name"          : f"del-config-{uuid.uuid4().hex[:8]}",
            "source_conn_id": source_conn["id"],
            "target_conn_id": target_conn["id"],
            "table_mappings": [{"source_table": "t", "target_table": "t"}]
        })
        cid = r.json()["id"]
        r = client.delete(f"/api/v1/configs/{cid}")
        assert r.status_code == 204


class TestRunAPI:
    def test_list_runs(self, client):
        r = client.get("/api/v1/runs")
        assert r.status_code == 200

    def test_get_run_not_found(self, client):
        r = client.get("/api/v1/runs/nonexistent")
        assert r.status_code == 404

    def test_run_status_filter(self, client):
        r = client.get("/api/v1/runs?status=completed")
        assert r.status_code == 200

    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code in (200, 503)
