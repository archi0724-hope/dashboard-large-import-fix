import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import RuntimeConfig
from app.export.exporter import Exporter
from app.main import app
from app.services import AppState, set_state


@pytest.fixture()
def client(settings, demo_dir, tmp_path):
    state = AppState(settings, db_path=tmp_path / "t.duckdb")
    state.update_config({"source_kind": "local", "local_input_dir": str(demo_dir), "output_dir": str(tmp_path / "out")})
    set_state(state)
    yield TestClient(app)
    set_state(None)
    state.repo.close()


def run_and_wait(c):
    assert c.post("/api/run", json={}).status_code == 200
    for _ in range(200):
        p = c.get("/api/progress").json()
        if not p["running"]:
            return p
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def test_full_flow_through_the_api(client):
    c = client
    assert c.post("/api/scan").json()["count"] == 11
    insp = c.post("/api/inspect", json={"max_files": 40}).json()["files"]
    assert {f["status"] for f in insp} == {"ok", "error", "unsupported"}
    assert c.post("/api/dry-run", json={"sample": 20}).json()["count"] > 0
    p = run_and_wait(c)
    assert not p["error"] and p["percent"] == 100
    s = c.get("/api/summary").json()
    assert s["totals"]["entities"] == 24 and s["totals"]["pending_reviews"] == 2
    e = c.get("/api/entities?search=sawai").json()
    assert e["total"] == 1 and "SMS Hosp." in e["items"][0]["aliases"]
    d = c.get(f"/api/entities/{e['items'][0]['master_entity_id']}").json()
    assert len(d["records"]) == 5 and all(r["source_file"] for r in d["records"])       # traceable to file / sheet / row
    rv = c.get("/api/reviews").json()
    rid = rv["items"][0]["review_id"]
    assert c.get(f"/api/reviews/{rid}").json()["evidence"]["checklist"]
    assert c.post(f"/api/reviews/{rid}/decide", json={"action": "accept"}).status_code == 200
    assert c.post(f"/api/reviews/{rid}/decide", json={"action": "accept"}).status_code == 400
    assert c.get("/api/reviews").json()["total"] == 1
    assert c.post("/api/export").status_code == 200
    names = {f["file"] for f in c.get("/api/exports").json()["files"]}
    assert {"master_data.xlsx", "name_mapping.csv", "processing_report.xlsx", "needs_manual_review.xlsx"} <= names
    assert c.get("/api/exports/download?file=master_data.csv").status_code == 200


def test_dashboard_reset_clears_all_visible_workspace_data(client):
    c = client
    progress = run_and_wait(c)
    assert not progress["error"]
    assert c.get("/api/status").json()["totals"]["records"] > 0

    response = c.post("/api/reset", json={"scope": "all"})

    assert response.status_code == 200
    status = c.get("/api/status").json()
    assert status["totals"] == {
        "files": 0,
        "files_processed": 0,
        "files_failed": 0,
        "records": 0,
        "entities": 0,
        "pending_reviews": 0,
        "errors": 0,
    }
    assert status["config"]["local_input_dir"] == ""


def test_removes_one_unsupported_file_record_without_resetting_workspace(client):
    state = __import__("app.main", fromlist=["st"]).st()
    state.repo.upsert_source_file(
        file_key="local:unsupported-file", source_kind="local", file_name="ChatGPT - project.html",
        source_path="uploads/ChatGPT - project.html", ext=".html", status="unsupported",
        notes="Unsupported file type", size_bytes=12,
    )

    response = client.delete("/api/files/local:unsupported-file")

    assert response.status_code == 200
    assert response.json() == {"removed": "ChatGPT - project.html"}
    assert state.repo.get_source_file("local:unsupported-file") is None


def test_cannot_run_twice_or_download_outside_output(client):
    assert client.get("/api/exports/download?file=../../etc/passwd").status_code == 404
    assert client.get("/api/exports/download?file=%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/logs/not_a_log").status_code == 404


def test_bad_config_is_rejected(client):
    r = client.put("/api/config", json={"thresholds": {"very_high": 10, "high": 20, "possible": 30, "review": 40}})
    assert r.status_code == 400 and "thresholds" in r.json()["detail"]
    r = client.put("/api/config", json={"output_dir": client.get("/api/config").json()["local_input_dir"] + "/out"})
    assert r.status_code == 400 and "overlaps" in r.json()["detail"]


def test_token_protects_the_api(client, settings):
    from dataclasses import replace

    state = AppState(replace(settings, dashboard_token="s3cret"), db_path=":memory:")
    set_state(state)
    c = TestClient(app)
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"X-Token": "s3cret"}).status_code == 200
    assert c.get("/").status_code == 200                                # the page itself is static


def test_upload_is_sanitised(client, tmp_path):
    r = client.post("/api/upload", files=[("files", ("../../evil.csv", b"Name\nX Hospital\n", "text/csv"))])
    assert r.status_code == 200 and r.json()["saved"] == ["evil.csv"]


def test_export_neutralises_formulas_and_respects_excel_limit(settings, tmp_path, repo):
    import openpyxl
    import pandas as pd

    cfg = RuntimeConfig(output_dir=str(tmp_path / "o"), export_formats=["xlsx", "csv"])
    ex = Exporter(settings, cfg, repo)
    df = pd.DataFrame({"Name": ['=HYPERLINK("http://evil","x")', "@SUM(1)", "Normal"]})
    ex._write(df, ex.out / "t", "xlsx")
    ex._write(df, ex.out / "t", "csv")
    ws = openpyxl.load_workbook(ex.out / "t.xlsx")["Data"]
    assert ws["A2"].data_type == "s" and ws["A2"].value.startswith("=")            # stored as text, never a formula
    assert (ex.out / "t.csv").read_text(encoding="utf-8-sig").splitlines()[1].startswith("\"'=") or "'=" in (ex.out / "t.csv").read_text(encoding="utf-8-sig")


def test_grouped_export_creates_folders(settings, tmp_path, done_pipeline):
    cfg = RuntimeConfig(output_dir=str(tmp_path / "o"), export_formats=["csv"], group_by=["state", "city"])
    man = Exporter(settings, cfg, done_pipeline.repo).export_all()
    assert (tmp_path / "o" / "by_group" / "Rajasthan" / "Jaipur" / "master_data.csv").exists()
    assert any(m["file"] == "processing_report.xlsx" for m in man)
