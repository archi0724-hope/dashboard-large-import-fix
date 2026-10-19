import subprocess
import sys
import zipfile

from build_deploy_bundle import build


def test_deployment_bundle_imports_without_source_checkout(tmp_path):
    bundle = build(tmp_path / "deploy.zip")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(extracted)

    # Isolated mode and a separate working directory prevent the checkout from
    # supplying modules missing from the deployment artifact.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "import import_service, catalogue_extraction, storage, exports, drive_import, vendor_core; "
            "import asgi_app; from vendor_dashboard.web import application, health; "
            "modules = (import_service, catalogue_extraction, storage, exports, drive_import, vendor_core); "
            "assert all(Path(m.__file__).parent == Path(sys.argv[1]) for m in modules); "
            "assert Path(asgi_app.__file__).parent == Path(sys.argv[1]); "
            "assert Path(application.__file__).is_relative_to(Path(sys.argv[1])); "
            "assert Path(health.__file__).is_relative_to(Path(sys.argv[1]))",
            str(extracted),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
