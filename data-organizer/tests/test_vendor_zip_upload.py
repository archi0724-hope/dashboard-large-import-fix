import time
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.vendor_zip_upload as uploads
from app import main
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    vendor_dir = Path(__file__).resolve().parents[1] / "vendor_dashboard"
    monkeypatch.syspath_prepend(str(vendor_dir))
    monkeypatch.setenv("APP_PASSWORD", "synthetic-upload-password")
    from storage import Store

    store = Store(tmp_path / "store")
    monkeypatch.setattr(main, "catalogue_store", lambda: store)
    with uploads._lock:
        assert uploads._active is None
        uploads._jobs.clear()
    yield (
        TestClient(app),
        store,
        {"X-Upload-Token": uploads.issue_upload_token("synthetic-upload-password")},
    )
    for _ in range(500):
        with uploads._lock:
            if uploads._active is None:
                break
        time.sleep(0.01)
    assert uploads._active is None


def zip_body(name="Alpha Medical/GST.txt", body=b"Synthetic GST document"):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, body)
    return output.getvalue()


def wait_for_job(client, headers, job_id):
    for _ in range(500):
        response = client.get("/api/vendor-uploads/" + job_id, headers=headers)
        assert response.status_code == 200
        state = response.json()
        if state["status"] in {"done", "failed"}:
            return state
        time.sleep(0.01)
    raise AssertionError("import did not finish")


def test_disk_upload_saves_original_bytes_and_skips_duplicates(client):
    c, store, headers = client
    body = zip_body()
    paths = []
    factory = uploads.tempfile.NamedTemporaryFile

    def track_file(**kwargs):
        result = factory(**kwargs)
        paths.append(Path(result.name))
        return result

    with patch.object(uploads.tempfile, "NamedTemporaryFile", side_effect=track_file):
        for expected in ({"saved": 1, "duplicates": 0}, {"saved": 0, "duplicates": 1}):
            response = c.post(
                "/api/vendor-uploads?filename=vendors.zip",
                headers=headers,
                content=(body[i : i + 32] for i in range(0, len(body), 32)),
            )
            assert response.status_code == 200
            state = wait_for_job(c, headers, response.json()["job_id"])
            assert state["status"] == "done"
            for key, value in expected.items():
                assert state["result"][key] == value
    assert len(store.documents()) == 1
    assert (
        store.read_bytes(int(store.documents().iloc[0]["id"]))
        == b"Synthetic GST document"
    )
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("token", ["", "garbage", "1:nonce:bad-signature"])
def test_upload_requires_scoped_authorization(client, token):
    c, store, _ = client
    response = c.post(
        "/api/vendor-uploads?filename=vendors.zip",
        headers={"X-Upload-Token": token},
        content=zip_body(),
    )
    assert response.status_code == 401
    assert store.documents().empty


def test_expired_token_is_rejected(client, monkeypatch):
    c, _, headers = client
    monkeypatch.setattr(
        uploads.time, "time", lambda: time.time_ns() / 1e9 + uploads.TOKEN_LIFETIME + 1
    )
    assert (
        c.post(
            "/api/vendor-uploads?filename=vendors.zip",
            headers=headers,
            content=zip_body(),
        ).status_code
        == 401
    )


@pytest.mark.parametrize(
    "filename", ["C:/Users/user/data.zip", "../data.zip", "data.txt"]
)
def test_rejects_computer_paths_and_non_zip_filenames(client, filename):
    c, store, headers = client
    response = c.post(
        "/api/vendor-uploads",
        params={"filename": filename},
        headers=headers,
        content=zip_body(),
    )
    assert response.status_code == 422
    assert store.documents().empty


def test_bad_archive_is_rejected_and_slot_is_released(client):
    c, _, headers = client
    assert (
        c.post(
            "/api/vendor-uploads?filename=bad.zip", headers=headers, content=b"not zip"
        ).status_code
        == 422
    )
    assert uploads._active is None
    response = c.post(
        "/api/vendor-uploads?filename=valid.zip", headers=headers, content=zip_body()
    )
    assert response.status_code == 200
    assert wait_for_job(c, headers, response.json()["job_id"])["status"] == "done"


def test_declared_and_streamed_size_limits(client, monkeypatch):
    c, _, headers = client
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 50)
    body = zip_body()
    assert (
        c.post(
            "/api/vendor-uploads?filename=large.zip", headers=headers, content=body
        ).status_code
        == 413
    )
    assert (
        c.post(
            "/api/vendor-uploads?filename=large.zip",
            headers=headers,
            content=(body[i : i + 16] for i in range(0, len(body), 16)),
        ).status_code
        == 413
    )
    assert uploads._active is None


def test_concurrent_upload_and_other_session_status_are_blocked(client):
    c, _, headers = client
    with uploads._lock:
        uploads._active = "synthetic-active-job"
    try:
        assert (
            c.post(
                "/api/vendor-uploads?filename=vendors.zip",
                headers=headers,
                content=zip_body(),
            ).status_code
            == 409
        )
    finally:
        uploads._active = None
    uploads._jobs["synthetic-done"] = {"status": "done", "owner": "different-session"}
    assert (
        c.get("/api/vendor-uploads/synthetic-done", headers=headers).status_code == 404
    )


def test_zip_extraction_safeguards_are_retained(client):
    c, store, headers = client
    response = c.post(
        "/api/vendor-uploads?filename=unsafe.zip",
        headers=headers,
        content=zip_body("../escape.txt"),
    )
    state = wait_for_job(c, headers, response.json()["job_id"])
    assert state["status"] == "done"
    assert state["result"]["saved"] == 0
    assert state["result"]["issues"] > 0
    assert store.documents().empty


def test_upload_page_does_not_require_or_embed_a_password(client):
    c, _, _ = client
    response = c.get("/vendor-upload")
    assert response.status_code == 200
    assert "synthetic-upload-password" not in response.text
    assert 'type="file"' in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
