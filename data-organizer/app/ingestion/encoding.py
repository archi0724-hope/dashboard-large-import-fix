"""Encoding and delimiter detection for text-like files (CSV, TSV, TXT)."""
from __future__ import annotations

import csv
from pathlib import Path

CANDIDATE_DELIMS = [",", ";", "\t", "|"]


def detect_encoding(path: Path, sample_bytes: int = 262_144) -> tuple[str, float]:
    with open(path, "rb") as fh:
        raw = fh.read(sample_bytes)
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", 1.0
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16", 1.0
    if not raw:
        return "utf-8", 1.0
    # a strict UTF-8 decode of the sample (allow the sample to cut a multi-byte char at the end)
    for cut in range(0, 4):
        try:
            (raw[: len(raw) - cut] if cut else raw).decode("utf-8")
            return "utf-8", 1.0
        except UnicodeDecodeError:
            continue
    # Windows exports (Excel "CSV", Notepad "ANSI") are almost always cp1252: accept it when it decodes cleanly,
    # because statistical detectors are unreliable on short samples.
    try:
        raw.decode("cp1252")
        return "cp1252", 0.8
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best and best.encoding:
            enc = best.encoding.lower()
            return ("utf-8" if enc == "ascii" else enc), float(max(0.0, 1.0 - (best.chaos or 0)))
    except Exception:
        pass
    return "latin-1", 0.3


def read_head(path: Path, encoding: str, max_chars: int = 200_000) -> str:
    with open(path, "rb") as fh:
        raw = fh.read(max_chars * 2)
    return raw.decode(encoding, errors="replace")[:max_chars]


def detect_delimiter(text: str) -> str | None:
    """Return the delimiter used by a consistently delimited text, else None."""
    lines = [ln for ln in text.splitlines() if ln.strip()][:60]
    if len(lines) < 2:
        return None
    try:
        dialect = csv.Sniffer().sniff("\n".join(lines[:30]), delimiters="".join(CANDIDATE_DELIMS))
        d = dialect.delimiter
        counts = [ln.count(d) for ln in lines]
        if counts and sum(1 for c in counts if c >= 1) / len(counts) >= 0.7:
            return d
    except csv.Error:
        pass
    best, best_score = None, 0.0
    for d in CANDIDATE_DELIMS:
        counts = [ln.count(d) for ln in lines]
        nz = [c for c in counts if c > 0]
        if not nz or len(nz) / len(counts) < 0.7:
            continue
        mode = max(set(nz), key=nz.count)
        score = (nz.count(mode) / len(counts)) * (1 + min(mode, 8) / 8)
        if score > best_score:
            best, best_score = d, score
    return best
