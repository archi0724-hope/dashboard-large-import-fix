import json

import pytest

from app.config import ConfigError, parse_drive_folder_id
from app.drive.google_drive import DriveSource
from app.pipeline import Pipeline


class FakeReq:
    def __init__(self, payload): self.payload = payload
    def execute(self, num_retries=0): return self.payload


class FakeFiles:
    """Two-level Drive: root has a sheet-like xlsx, a Google Doc, a shortcut loop and a sub-folder."""
    def __init__(self):
        self.tree = {
            "ROOT": [{"id": "f1", "name": "list.csv", "mimeType": "text/csv", "size": "40", "modifiedTime": "2026-01-01T00:00:00Z"},
                     {"id": "d1", "name": "Sub", "mimeType": "application/vnd.google-apps.folder"},
                     {"id": "g1", "name": "Notes", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-01-02T00:00:00Z"},
                     {"id": "s1", "name": "loop", "mimeType": "application/vnd.google-apps.shortcut",
                      "shortcutDetails": {"targetId": "ROOT", "targetMimeType": "application/vnd.google-apps.folder"}}],
            "d1": [{"id": "f2", "name": "b.csv", "mimeType": "text/csv", "size": "30", "modifiedTime": "2026-01-03T00:00:00Z"}],
        }
        self.calls = []
    def list(self, q, **kw):
        parent = q.split("'")[1]
        self.calls.append(parent)
        return FakeReq({"files": self.tree.get(parent, [])})


class FakeService:
    def __init__(self): self._f = FakeFiles()
    def files(self): return self._f


def test_drive_url_parsing():
    assert parse_drive_folder_id("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp?usp=sharing") == "1AbCdEfGhIjKlMnOp"
    assert parse_drive_folder_id("1AbCdEfGhIjKlMnOpQ") == "1AbCdEfGhIjKlMnOpQ"
    with pytest.raises(ConfigError):
        parse_drive_folder_id("not a folder link!")


def test_drive_walk_keeps_folders_handles_docs_and_shortcut_loops(settings):
    svc = FakeService()
    files = list(DriveSource(settings, "ROOT", service=svc).scan())
    paths = sorted(f.path for f in files)
    assert paths == ["Notes.docx", "Sub/b.csv", "list.csv"]                    # sub-folder path kept, Doc exported as .docx
    assert svc._f.calls.count("ROOT") == 1                                      # the loop shortcut is not followed twice
    assert all(f.file_key.startswith("drive:") and f.supported for f in files)


def test_drive_source_is_readonly_scope():
    from app.drive.google_drive import SCOPES

    assert SCOPES == ["https://www.googleapis.com/auth/drive.readonly"]

