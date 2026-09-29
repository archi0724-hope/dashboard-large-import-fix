"""Find files in a local input folder (the Drive equivalent lives in ``app/drive``)."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import SourceFile
from .registry import SUPPORTED_EXTENSIONS

_IGNORED_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


def is_ignorable(name: str) -> bool:
    low = name.lower()
    return low.startswith("~$") or low.startswith(".") or low in _IGNORED_NAMES or low.endswith((".tmp", ".crdownload"))


def scan_local_folder(root: str | Path) -> Iterator[SourceFile]:
    """Recursively yield every regular file below ``root`` (sub-folders included, symlinked dirs not followed)."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Input folder does not exist: {root}")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if is_ignorable(fn):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            ext = p.suffix.lower()
            yield SourceFile(
                file_key="local:" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16],
                name=fn,
                path=rel,
                local_path=p,
                origin="local",
                ext=ext,
                size=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                supported=ext in SUPPORTED_EXTENSIONS,
            )
