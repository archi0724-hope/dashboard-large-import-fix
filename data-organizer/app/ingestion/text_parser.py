"""Turn free-text lines (TXT, Word paragraphs, PDF text, OCR output) into candidate records.

Two modes, chosen per text block:

* label mode   ``Name: ABC Hospital`` / ``Address: ...`` / ``Phone: ...`` lines are grouped into one record
               per ``Name`` label;
* line mode    one line = one record: emails / URLs / phones / IDs / PIN codes are lifted out, the remaining text
               is split on ``|`` ``;`` ``,`` (in that order of preference); the first part is the name, the rest
               are classified as city / state / address using the gazetteer.

Every line ends up as a record, even when no name can be recognised (it then carries ``name=None`` and is
reported as a data-quality issue) - nothing is silently dropped. The full original text is always kept.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from ..cleaning.normalizer import Gazetteer, basic_clean, normalize_text
from ..schema_mapping import SchemaMapper, join_values

_LABELS_PHONE = r"ph(?:one)?|tel(?:ephone)?|mob(?:ile)?|contact|cell|fax|whatsapp"
_EMAIL = re.compile(r"(?:\b(?:e-?mail|mail)\b\.?\s*(?:id)?\s*[:\-]?\s*)?(?P<v>[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+)", re.I)
_URL = re.compile(r"(?:\b(?:web(?:site)?|url)\b\.?\s*[:\-]?\s*)?(?P<v>(?:https?://|www\.)[^\s,;|]+)", re.I)
_GST = re.compile(r"(?:\b(?:gstin|gst)\b\.?\s*(?:no\.?|number)?\s*[:\-]?\s*)?(?P<v>\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b)")
_UDYAM = re.compile(r"(?:\budyam\b[^:\n]{0,25}[:\-]?\s*)?(?P<v>\bUDYAM-?[A-Z]{2}-?\d{2}-?\d{7}\b)", re.I)
_CIN = re.compile(r"(?:\bcin\b\.?\s*[:\-]?\s*)?(?P<v>\b[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}\b)")
_PHONE = re.compile(rf"(?:\b(?:{_LABELS_PHONE})\b\.?\s*(?:no\.?|number)?\s*[:\-]?\s*)?(?P<v>(?<![\w])\+?\d[\d\s\-()]{{7,17}}\d(?!\w))", re.I)
_PIN = re.compile(r"(?:\bpin(?:\s*code)?\b\.?\s*[:\-]?\s*)?(?<!\d)(?P<v>[1-9]\d{5})(?!\d)", re.I)
_LIST_MARKER = re.compile(r"^\s*(?:\(?\d{1,4}[.)]\s+|[•\u2022\u25aa\u00b7*\-–]\s+|\(?[a-z][.)]\s+)")
_KV = re.compile(r"^\s*(?P<label>[^:|]{2,40}?)\s*:\s*(?P<value>.+?)\s*$")
_EDGE_PUNCT = re.compile(r"^[\s,;:|\-–.]+|[\s,;:|\-–]+$")


@dataclass
class ParsedRecord:
    row: int                                  # source line number (first line of the record)
    fields: dict[str, str | None] = field(default_factory=dict)
    data: dict[str, str] = field(default_factory=dict)   # everything we know, for original_data


def _lift(pattern: re.Pattern, text: str, min_digits: int = 0, max_digits: int = 99) -> tuple[list[str], str]:
    found: list[str] = []

    def repl(m: re.Match) -> str:
        v = m.group("v")
        digits = len(re.sub(r"\D", "", v))
        if min_digits and not (min_digits <= digits <= max_digits):
            return m.group(0)
        found.append(v.strip())
        return " "

    return found, pattern.sub(repl, text)


def _clean_name(part: str) -> str | None:
    part = _LIST_MARKER.sub("", part)
    part = _EDGE_PUNCT.sub("", part)
    part = re.sub(r"\s+", " ", part).strip()
    return part if re.search(r"[^\W\d_]", part) else None


def parse_line(text: str, gaz: Gazetteer) -> ParsedRecord:
    original = basic_clean(text)
    work = original
    fields: dict[str, str | None] = {}

    emails, work = _lift(_EMAIL, work)
    urls, work = _lift(_URL, work)
    regs: list[str] = []
    for pat in (_GST, _UDYAM, _CIN):
        found, work = _lift(pat, work)
        regs += found
    phones, work = _lift(_PHONE, work, 8, 13)
    pins, work = _lift(_PIN, work)

    parts: list[str]
    for sep in ("|", "\t", ";", ","):
        if sep in work:
            parts = [p for p in (x.strip() for x in work.split(sep)) if p]
            break
    else:
        m = re.split(r"\s[-–—]\s", work)
        parts = [p for p in (x.strip() for x in m) if p] if len(m) > 1 else [work.strip()]
    parts = [p for p in parts if _EDGE_PUNCT.sub("", p)]

    if len(parts) >= 2 and re.fullmatch(r"\d{1,5}[.)]?", parts[0].strip()):
        parts = parts[1:]                       # serial number column
    if parts and "," in parts[0]:               # "SMS Hosp., Jaipur | phone": a trailing known place is not part of the name
        head, _, tail = parts[0].rpartition(",")
        if gaz.state_lookup.get(normalize_text(tail)) or gaz.normalize_city(tail) in gaz.city_state:
            parts = [head.strip(), tail.strip()] + parts[1:]
    name = _clean_name(parts[0]) if parts else None
    city = state = None
    addr: list[str] = []
    for p in parts[1:]:
        p = _EDGE_PUNCT.sub("", p)
        if not p:
            continue
        n = normalize_text(p)
        if gaz.state_lookup.get(n) and not state:
            state = p
        elif gaz.normalize_city(p) in gaz.city_state and not city:
            city = p
        else:
            addr.append(p)

    if name:
        fields["name"] = name
    if addr:
        fields["address"] = ", ".join(addr)
    if city:
        fields["city"] = city
    if state:
        fields["state"] = state
    if pins:
        fields["pincode"] = pins[0]
    if phones:
        fields["phone"] = " / ".join(phones)
    if emails:
        fields["email"] = "; ".join(emails)
    if urls:
        fields["website"] = " ".join(urls)
    if regs:
        fields["registration_id"] = "; ".join(regs)
    return ParsedRecord(row=0, fields=fields, data={"text": original})


def _kv_field(mapper: SchemaMapper, label: str) -> str | None:
    m = mapper.map_header(label)
    return m.field if m.field and m.method != "none" else None


def parse_text_block(lines: Iterable[tuple[int, str, dict]], mapper: SchemaMapper, gaz: Gazetteer) -> tuple[list[ParsedRecord], str]:
    """Return (records, mode) where mode is 'label' or 'line'."""
    items = [(n, basic_clean(t), ctx) for n, t, ctx in lines if basic_clean(t)]
    labelled = 0
    has_name_label = False
    for _, t, _ in items:
        m = _KV.match(t)
        if m and (f := _kv_field(mapper, m.group("label"))):
            labelled += 1
            has_name_label |= f == "name"
    if labelled >= 3 and has_name_label:
        return _label_mode(items, mapper), "label"
    out: list[ParsedRecord] = []
    for n, t, ctx in items:
        rec = parse_line(t, gaz)
        rec.row = n
        rec.data.update({k: str(v) for k, v in ctx.items() if k in ("heading", "style", "ocr_confidence")})
        if ctx.get("is_heading"):
            rec.data["is_heading"] = "true"
        out.append(rec)
    return out, "line"


def _label_mode(items: list[tuple[int, str, dict]], mapper: SchemaMapper) -> list[ParsedRecord]:
    records: list[ParsedRecord] = []
    cur: ParsedRecord | None = None
    values: dict[str, list[str]] = {}

    def flush() -> None:
        nonlocal cur, values
        if cur is not None:
            for fld, vals in values.items():
                cur.fields[fld] = join_values(vals, fld)
            records.append(cur)
        cur, values = None, {}

    for n, t, ctx in items:
        m = _KV.match(t)
        fld = _kv_field(mapper, m.group("label")) if m else None
        if fld:
            if fld == "name" and cur is not None and "name" in values:
                flush()
            if cur is None:
                cur = ParsedRecord(row=n, data={})
            values.setdefault(fld, []).append(m.group("value"))
            cur.data[m.group("label")] = m.group("value")
        else:
            if cur is None:
                cur = ParsedRecord(row=n, data={})
            cur.data.setdefault("unlabelled_lines", "")
            cur.data["unlabelled_lines"] = (cur.data["unlabelled_lines"] + " | " + t).strip(" |")
    flush()
    return records
