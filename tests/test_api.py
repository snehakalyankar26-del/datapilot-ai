import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import settings


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _ids(client):
    return {d["name"]: d["id"] for d in client.get("/api/datasets").json()}


def test_health_and_headers(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    for h in ("content-security-policy", "x-frame-options", "x-content-type-options", "x-request-id"):
        assert h in r.headers


def test_samples_loaded(client):
    assert {"food_delivery_orders", "hospital_admissions", "energy_emissions"} <= set(_ids(client))


def test_ask_and_export(client):
    ds = _ids(client)["food_delivery_orders"]
    r = client.post("/api/ask", json={"dataset_id": ds, "question": "Average rating by cuisine"}).json()
    assert r["status"] == "answered" and r["result"]["row_count"] == 6
    csv_r = client.post("/api/export", json={"dataset_id": ds, "sql": r["result"]["sql"]})
    assert csv_r.status_code == 200 and csv_r.text.startswith("cuisine,avg_rating")


def test_export_rejects_malicious_sql(client):
    ds = _ids(client)["food_delivery_orders"]
    r = client.post("/api/export", json={"dataset_id": ds, "sql": "DELETE FROM food_delivery_orders"})
    assert r.status_code == 400


def test_upload_roundtrip_with_formula_and_pii(client):
    csv = b"name,amount,contact_email\n=CMD|calc,10,bob@x.com\nalice,20,al@y.com\n"
    r = client.post("/api/datasets", files={"file": ("people.csv", csv, "text/csv")})
    assert r.status_code == 200
    d = r.json()
    assert "contact_email" in d["pii_columns"]
    exp = client.post("/api/export", json={"dataset_id": d["id"], "sql": "SELECT name FROM people"})
    assert "'=CMD|calc" in exp.text
    assert client.delete(f"/api/datasets/{d['id']}").status_code == 200
    assert client.get(f"/api/datasets/{d['id']}").status_code == 404


def test_upload_rejects_bad_files(client):
    r = client.post("/api/datasets", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_selftest_blocks_everything(client):
    r = client.get("/api/security/selftest").json()
    assert r["passed"] == r["total"] and r["data_intact"]


def test_metrics(client):
    m = client.get("/api/metrics").json()
    assert m["requests"] >= 1 and "p95" in m["latency_ms"]


def test_api_key_enforced(client):
    object.__setattr__(settings, "api_keys", ["secret-key"])  # settings is frozen; patch for this test only
    try:
        assert client.get("/api/datasets").status_code == 401
        assert client.get("/api/datasets", headers={"X-API-Key": "secret-key"}).status_code == 200
        assert client.get("/api/health").status_code == 200  # public
    finally:
        object.__setattr__(settings, "api_keys", [])


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "limiter", main.RateLimiter(2))
    ds = _ids(client)["energy_emissions"]
    codes = [client.post("/api/ask", json={"dataset_id": ds, "question": "Quarterly cost"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
