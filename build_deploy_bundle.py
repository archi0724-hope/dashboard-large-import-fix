"""Build a source-only deployment ZIP without local data or secrets."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / "vendor_dashboard_deploy.zip"
FILES = [
    "app.py",
    "vendor_core.py",
    "storage.py",
    "exports.py",
    "import_service.py",
    "drive_import.py",
    "requirements.txt",
    "DEPLOY.md",
    ".gitignore",
    ".streamlit/config.toml",
    ".streamlit/secrets.toml.example",
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
