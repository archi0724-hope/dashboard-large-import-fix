import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json


def test_live_wrapper_health_without_opening_vendor_storage(tmp_path):
    root = Path(__file__).resolve().parents[1]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["VENDOR_DATA_DIR"] = str(tmp_path / "unused-storage")
    env["APP_PASSWORD"] = "test-only-password"
    env["CLOUD_DEPLOYMENT"] = "true"
    url = f"http://127.0.0.1:{port}"
    with (tmp_path / "server.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(root / "asgi_app.py"),
                "--server.address",
                "127.0.0.1",
                "--server.port",
                str(port),
                "--server.headless",
                "true",
                "--server.fileWatcherType",
                "none",
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(url + "/api/health", timeout=1) as response:
                        assert response.status == 200
                        assert response.headers.get_content_type() == "application/json"
                        assert response.headers["Cache-Control"] == "no-store"
                        assert json.loads(response.read()) == {"status": "ok"}
                    break
                except URLError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(
                            "Server did not become healthy:\n" + log.read()
                        )
                    time.sleep(0.1)
            with urlopen(
                Request(url + "/api/health", method="HEAD"), timeout=3
            ) as response:
                assert response.status == 200 and response.read() == b""
            try:
                urlopen(Request(url + "/api/health", method="POST"), timeout=3)
            except HTTPError as error:
                assert error.code == 405
            else:
                raise AssertionError("Health endpoint must reject POST")
            assert not (tmp_path / "unused-storage").exists()
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
