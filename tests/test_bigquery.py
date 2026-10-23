"""Unit tests for Google Cloud BigQuery engine & API endpoints."""
import pytest
from fastapi.testclient import TestClient

from app.bigquery_engine import BigQueryEngine, bq_engine
from app.config import settings
from app.datastore import DataStore
from app.main import app
from app.security import SecurityError

client = TestClient(app)


def test_bigquery_blocks_destructive_keywords():
    engine = BigQueryEngine()
    for kw in ["DROP TABLE t", "DELETE FROM t", "UPDATE t SET a=1", "EXECUTE IMMEDIATE 'x'", "TRUNCATE TABLE t"]:
        with pytest.raises(SecurityError):
            engine.validate_bigquery_sql(kw)


def test_bigquery_allows_select_and_cte():
    engine = BigQueryEngine()
    assert engine.validate_bigquery_sql("SELECT city, SUM(amount) FROM sales GROUP BY city")
    assert engine.validate_bigquery_sql("WITH ranked AS (SELECT * FROM sales) SELECT * FROM ranked")


def test_bigquery_blocks_multiple_statements():
    engine = BigQueryEngine()
    with pytest.raises(SecurityError):
        engine.validate_bigquery_sql("SELECT 1; SELECT 2")


def test_bigquery_dry_run_cost_estimation():
    engine = BigQueryEngine()
    res = engine.dry_run("SELECT city, amount FROM sales", "sales")
    assert res["valid"] is True
    assert res["bytes_processed"] > 0
    assert res["cost_usd"] >= 0.0


def test_bigquery_history_logging():
    ds = DataStore().load_csv_bytes(b"city,amount\nDelhi,100\nMumbai,200\n", "test.csv")
    res = ds.execute("SELECT city, amount FROM test")
    assert "bytes_processed" in res
    assert "cost_usd" in res
    assert res["engine"] == "BigQuery Analytics"
    history = bq_engine.get_audit_history()
    assert len(history) > 0
    assert history[0]["dataset"] == "test"


def test_api_benchmark_endpoint():
    resp = client.get("/api/benchmark")
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert data["summary"]["cases"] == 20
    assert data["summary"]["accuracy"] == 1.0


def test_api_settings_keys_endpoint():
    resp = client.post("/api/settings/keys", json={"gemini_api_key": "test_key", "model": "gemini-2.5-flash"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "updated"
    assert data["llm_enabled"] is True
    # Reset back to empty for offline tests
    client.post("/api/settings/keys", json={"gemini_api_key": ""})


def test_api_bigquery_history_endpoint():
    resp = client.get("/api/bigquery/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert "history" in data
