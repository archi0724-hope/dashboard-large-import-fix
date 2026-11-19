"""Hosted readiness uses schema metadata, not repeated DDL or catalogue scans."""
import importlib.util
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def metadata_store(tmp_path, monkeypatch):
    dashboard = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(dashboard))
    spec = importlib.util.spec_from_file_location("cloud_startup_storage", dashboard / "storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store = module.Store(tmp_path)
    calls = []
    overrides = {}

    class MetadataQuery:
        def execute(self, sql, params=()):
            calls.append(sql)
            assert sql.lstrip().upper().startswith("SELECT"), "Ready schema must remain read-only"
            with sqlite3.connect(store.db_path) as db:
                db.row_factory = sqlite3.Row
                if "information_schema.columns" in sql:
                    rows = [{"table_name": table, "column_name": column["name"]}
                            for table in params for column in db.execute(f"PRAGMA table_info({table})")]
                    rows = [row for row in rows if (row["table_name"], row["column_name"]) != overrides.get("missing_column")
                            and row["table_name"] != overrides.get("missing_table")]
                elif "pg_indexes" in sql:
                    rows = [dict(row) for row in db.execute("SELECT name AS indexname FROM sqlite_master WHERE type='index'")]
                    rows = [row for row in rows if row["indexname"] != overrides.get("missing_index")]
                elif "catalogue_policy_version" in sql:
                    rows = [dict(row) for row in db.execute(sql)]
                    if "policy" in overrides:
                        rows = [{"value": overrides["policy"]}]
                else:
                    rows = [{"missing": overrides.get("missing_ids", False)}]
            return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)

    @contextmanager
    def connection():
        yield MetadataQuery()

    store.cloud = True
    monkeypatch.setattr(store, "connection", connection)
    return module, store, calls, overrides


def test_existing_cloud_schema_opens_with_bounded_read_only_checks(metadata_store, monkeypatch):
    _, store, calls, _ = metadata_store
    def unexpected(*args):
        raise AssertionError("Already-ready database must not repeat schema or data migrations")
    for name in ("_ensure_product_columns", "_ensure_canonical_ids", "_ensure_canonical_aliases", "apply_catalogue_policy", "backfill_product_records"):
        monkeypatch.setattr(store, name, unexpected)
    store.initialize()
    assert len(calls) <= 4
    assert not any("COUNT(" in sql.upper() or "ADVISORY" in sql.upper() for sql in calls)


@pytest.mark.parametrize("override", [
    {"missing_table": "vdd_product_import_rows"},
    {"missing_column": ("vdd_product_records", "classification_reason")},
    {"missing_index": "vdd_products_search"},
    {"policy": "previous-policy"},
    {"missing_ids": True},
])
def test_incomplete_schema_requires_existing_setup(metadata_store, override):
    _, store, _, overrides = metadata_store
    overrides.update(override)
    assert store._cloud_schema_ready() is False


def test_cloud_setup_fallback_still_runs_without_catalogue_count(metadata_store, monkeypatch):
    _, store, _, _ = metadata_store
    calls = []
    class Query:
        def execute(self, sql, params=()):
            calls.append(sql)
            return self
        def fetchall(self):
            return []
    @contextmanager
    def connection():
        yield Query()
    monkeypatch.setattr(store, "_cloud_schema_ready", lambda: False)
    monkeypatch.setattr(store, "connection", connection)
    policy = Mock()
    backfill = Mock(side_effect=AssertionError("Cloud startup must not reparse documents"))
    monkeypatch.setattr(store, "apply_catalogue_policy", policy)
    monkeypatch.setattr(store, "backfill_product_records", backfill)
    store.initialize()
    assert any("CREATE TABLE" in sql for sql in calls)
    assert any("pg_advisory_xact_lock" in sql for sql in calls)
    assert not any("COUNT(" in sql.upper() for sql in calls)
    policy.assert_called_once_with()
    backfill.assert_not_called()


def test_connection_failure_never_falls_back_to_schema_writes(metadata_store, monkeypatch):
    _, store, _, _ = metadata_store
    @contextmanager
    def unavailable():
        raise TimeoutError("synthetic database timeout")
        yield
    monkeypatch.setattr(store, "connection", unavailable)
    with pytest.raises(TimeoutError):
        store.initialize()


def test_existing_drive_index_opens_without_repeating_ddl(metadata_store, monkeypatch):
    from app.drive.index_store import DriveIndex
    module, store, calls, _ = metadata_store
    with monkeypatch.context() as local:
        local.setattr(store, "cloud", False)
        local.setattr(store, "connection", lambda: module.Store.connection(store))
        DriveIndex(store)
    calls.clear()
    DriveIndex(store)
    assert len(calls) <= 2
    assert all(sql.lstrip().startswith("SELECT") for sql in calls)


def test_drive_schema_missing_dedup_column_does_not_skip_setup(metadata_store, monkeypatch):
    from app.drive.index_store import DriveIndex
    module, store, _, overrides = metadata_store
    with monkeypatch.context() as local:
        local.setattr(store, "cloud", False)
        local.setattr(store, "connection", lambda: module.Store.connection(store))
        DriveIndex(store)
    overrides["missing_column"] = ("vdd_drive_records", "dedup_identity")
    index = object.__new__(DriveIndex)
    index.store = store
    assert index._cloud_schema_ready() is False
