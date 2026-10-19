"""Build a source-only deployment ZIP without local data or secrets."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / "vendor_dashboard_deploy.zip"
FILES = [
    "asgi_app.py",
    "app.py",
    "vendor_core.py",
    "storage.py",
    "exports.py",
    "catalogue_extraction.py",
    "import_service.py",
    "drive_import.py",
    "requirements.txt",
    "DEPLOY.md",
    ".gitignore",
    ".streamlit/config.toml",
    ".streamlit/secrets.toml.example",
    "vendor_dashboard/__init__.py",
    "vendor_dashboard/web/__init__.py",
    "vendor_dashboard/web/application.py",
    "vendor_dashboard/web/health.py",
    "vendor_dashboard/domain/__init__.py",
    "vendor_dashboard/services/__init__.py",
    "vendor_dashboard/infrastructure/__init__.py",
    "vendor_dashboard/jobs/__init__.py",
]


def build(output: Path = OUTPUT) -> Path:
    missing = [name for name in FILES if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError("Missing deployment files: " + ", ".join(missing))
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:
            bundle.write(ROOT / name, name)
        bundle.writestr(
            "CLOUD_DEPLOYMENT",
            "Production deployment marker. Configure APP_PASSWORD and DATABASE_URL in Streamlit Secrets.\n",
        )
    return output


if __name__ == "__main__":
    print(build())
