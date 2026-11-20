import asyncio
import time
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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


def test_actual_disk_capacity_is_checked_for_declared_and_streamed_uploads(client, monkeypatch):
    c, _, headers = client
    monkeypatch.setattr(uploads.shutil,'disk_usage',lambda folder:SimpleNamespace(free=uploads.DISK_RESERVE+50))
    body = zip_body()
    assert (
        c.post(
            "/api/vendor-uploads?filename=large.zip", headers=headers, content=body
        ).status_code
        == 507
    )
    monkeypatch.setattr(uploads.shutil,'disk_usage',Mock(side_effect=[
        SimpleNamespace(free=uploads.DISK_RESERVE+1000),SimpleNamespace(free=uploads.DISK_RESERVE+1)]))
    assert (
        c.post(
            "/api/vendor-uploads?filename=large.zip",
            headers=headers,
            content=(body[i : i + 16] for i in range(0, len(body), 16)),
        ).status_code
        == 507
    )
    assert uploads._active is None


def test_no_fixed_two_gib_transport_cap_and_capacity_requires_auth(client,monkeypatch):
    c,_,headers = client
    monkeypatch.setattr(uploads.shutil,'disk_usage',lambda folder:SimpleNamespace(free=4*1024**3+uploads.DISK_RESERVE))
    assert c.get('/api/vendor-uploads/capacity').status_code == 401
    capacity = c.get('/api/vendor-uploads/capacity',headers=headers).json()
    assert capacity['fixed_upload_limit_bytes'] is None
    response = c.post('/api/vendor-uploads?filename=large.zip',headers={**headers,'Content-Length':str(3*1024**3)},content=zip_body())
    assert response.status_code == 400  # Accepted the declared size, then detected the incomplete body.
    assert 'incomplete' in response.json()['detail']


def test_client_upload_id_recovers_result_and_cannot_overwrite_another_job(client):
    c,_,headers = client
    job_id = 'a'*32
    response = c.post('/api/vendor-uploads?filename=vendors.zip&upload_id='+job_id,headers=headers,content=zip_body())
    assert response.status_code == 200 and response.json()['job_id'] == job_id
    assert wait_for_job(c,headers,job_id)['status'] == 'done'
    assert c.post('/api/vendor-uploads?filename=vendors.zip&upload_id='+job_id,headers=headers,content=zip_body()).status_code == 409
    other = {'X-Upload-Token':uploads.issue_upload_token('synthetic-upload-password')}
    assert c.get('/api/vendor-uploads/'+job_id,headers=other).status_code == 404


def test_organizer_sign_in_can_create_scoped_upload_session(client,monkeypatch):
    c,_,_ = client
    monkeypatch.setattr('app.config.get_settings',lambda:SimpleNamespace(dashboard_token='synthetic-organizer-token'))
    assert c.post('/api/vendor-uploads/session').status_code == 401
    response = c.post('/api/vendor-uploads/session',headers={'X-Token':'synthetic-organizer-token'})
    assert response.status_code == 200
    scoped = {'X-Upload-Token':response.json()['token']}
    assert c.get('/api/vendor-uploads/capacity',headers=scoped).status_code == 200


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


def begin(c, headers, body, job_id="b" * 32):
    response = c.post("/api/vendor-uploads/begin", headers=headers, params={
        "upload_id": job_id, "filename": "vendors.zip", "size": len(body),
    })
    assert response.status_code == 200, response.text
    return job_id


def test_chunk_transfer_is_registered_before_bytes_and_resumes_without_duplicate_writes(client):
    c, store, headers = client
    body = zip_body()
    job = begin(c, headers, body)
    assert c.get(f"/api/vendor-uploads/{job}", headers=headers).json()["received"] == 0
    # A lost registration response can be retried with the same ID.
    assert begin(c, headers, body) == job
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=body[:40]).json()["received"] == 40
    # Lost chunk acknowledgement: retry cannot append the bytes twice.
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=body[:40]).status_code == 409
    assert c.post(f"/api/vendor-uploads/{job}/complete", headers=headers).status_code == 409
    state = c.get(f"/api/vendor-uploads/{job}", headers=headers).json()
    assert "path" not in state and "owner" not in state
    assert state["received"] == 40
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=40", headers=headers, content=body[40:]).json()["received"] == len(body)
    assert store.documents().empty  # No import before completion.
    assert c.post(f"/api/vendor-uploads/{job}/complete", headers=headers).status_code == 200
    assert wait_for_job(c, headers, job)["result"]["saved"] == 1
    assert c.post(f"/api/vendor-uploads/{job}/complete", headers=headers).status_code == 200
    assert store.read_bytes(int(store.documents().iloc[0]["id"])) == b"Synthetic GST document"
    assert not uploads._jobs[job]["path"].exists()


def test_cancel_partial_transfer_releases_disk_and_slot_and_checks_owner(client):
    c, _, headers = client
    body = zip_body()
    job = begin(c, headers, body)
    other = {"X-Upload-Token": uploads.issue_upload_token("synthetic-upload-password")}
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=other, content=body).status_code == 404
    assert c.delete(f"/api/vendor-uploads/{job}", headers=other).status_code == 404
    assert c.delete(f"/api/vendor-uploads/{job}", headers=headers).status_code == 200
    assert uploads._active is None
    assert not uploads._jobs[job]["path"].exists()


def test_invalid_chunk_and_archive_can_be_retried_without_stuck_slot(client, monkeypatch):
    c, _, headers = client
    job = begin(c, headers, b"invalid zip")
    monkeypatch.setattr(uploads, "CHUNK_BYTES", 4)
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=b"12345").status_code == 413
    monkeypatch.setattr(uploads, "CHUNK_BYTES", 1024)
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=b"invalid zip!").status_code == 422
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=b"invalid zip").status_code == 200
    assert c.post(f"/api/vendor-uploads/{job}/complete", headers=headers).status_code == 422
    assert uploads._active is None
    assert not uploads._jobs[job]["path"].exists()


def test_chunk_session_renewal_preserves_ownership_even_after_expiry(client, monkeypatch):
    c, _, headers = client
    job = begin(c, headers, zip_body())
    now = time.time()
    monkeypatch.setattr(uploads.time, "time", lambda: now + uploads.TOKEN_LIFETIME + 10)
    assert c.get(f"/api/vendor-uploads/{job}", headers=headers).status_code == 401
    response = c.post("/api/vendor-uploads/session", headers=headers)
    assert response.status_code == 200
    renewed = {"X-Upload-Token": response.json()["token"]}
    assert renewed != headers
    assert c.get(f"/api/vendor-uploads/{job}", headers=renewed).status_code == 200
    assert c.delete(f"/api/vendor-uploads/{job}", headers=renewed).status_code == 200
    monkeypatch.setattr(uploads.time, "time", lambda: now + uploads.TOKEN_LIFETIME + 24 * 3600 + 10)
    assert c.post("/api/vendor-uploads/session", headers=headers).status_code == 401


def test_chunk_capacity_does_not_apply_a_fixed_total_file_cap(client, monkeypatch):
    c, _, headers = client
    size = 51 * 1024**3
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=size + uploads.DISK_RESERVE))
    response = c.post("/api/vendor-uploads/begin", headers=headers, params={"upload_id": "c" * 32, "filename": "large.zip", "size": size})
    assert response.status_code == 200
    job = response.json()["job_id"]
    assert uploads._jobs[job]["path"].stat().st_size == 0  # No 51 GiB allocation in this test.
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _: SimpleNamespace(free=uploads.DISK_RESERVE))
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=b"data").status_code == 507
    assert c.get(f"/api/vendor-uploads/{job}", headers=headers).json()["received"] == 0
    assert c.delete(f"/api/vendor-uploads/{job}", headers=headers).status_code == 200


def test_disconnect_mid_chunk_keeps_confirmed_offset_and_allows_retry(client):
    from starlette.requests import ClientDisconnect

    c, _, headers = client
    body = zip_body()
    job = begin(c, headers, body)

    class InterruptedRequest:
        async def stream(self):
            yield body[:20]
            raise ClientDisconnect()

    owner = uploads._jobs[job]["owner"]
    with pytest.raises(ClientDisconnect):
        asyncio.run(uploads.upload_chunk(job, 0, InterruptedRequest(), owner))
    assert uploads._jobs[job]["received"] == 0
    assert uploads._jobs[job]["writing"] is False
    assert uploads._jobs[job]["path"].stat().st_size == 0
    assert c.put(f"/api/vendor-uploads/{job}/chunk?offset=0", headers=headers, content=body).status_code == 200
    assert c.post(f"/api/vendor-uploads/{job}/complete", headers=headers).status_code == 200
    assert wait_for_job(c, headers, job)["result"]["saved"] == 1
