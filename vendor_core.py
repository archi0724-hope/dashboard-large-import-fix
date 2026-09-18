"""Deterministic company/document classification for Vendor Document Desk.

No network calls, OCR, LLM, or personal names are hard-coded here.
Folder ownership wins over names inside filenames. Uncertain files are retained.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath
import re
import stat
import unicodedata
import zipfile
from typing import Iterable, Iterator

import pandas as pd

DOCUMENT_TYPES = ["Cancelled Cheque", "GST", "MD", "Udyam", "ISO", "PAN Card", "Form 16", "Aadhar", "Price", "Catalogue"]
LEGACY_DOCUMENT_TYPE_NAMES = {"ASF ISO": "ISO"}
MASTER_COMPANY_NAMES = (
    "A.S.F Universal Opthalmic", "A.S.F. UNIVERSAL LLP", "Adequate Electro", "AM SCIENTIFIC", "Amkay Products",
    "AMX MEDICAL SYSTEMS", "Annai Latex", "Aps Lab Catalogue", "BIOTROL", "Blucia Overseas",
    "BOMBAIM ESSENTIALS (Osm dudes)", "Bos Medicare surgical", "Cardinal Medical Catalogue", "CARDIO BEATS LLP",
    "Careray Catalogue", "Cephas", "CK Hygiene", "COLDCHAIN CONTROLS", "DEEPAK INDUSTRY", "Deval Enterprises",
    "DEVAY", "DIABETIK FOOT CARE", "Global Scientific India", "Global Scientific Lab", "GLOZZY", "GULATI INDUSTRIES",
    "HANSCURE LIFESCIENCE", "Hanscure Multiple", "HARI OM SURGICAL", "Hari Om Surgicals Catalogue", "HS Medi",
    "ILLUMIX LED LIGHTNING PRIVATE LIMITED", "KDS Enterprise Kaarya", "KUKU MEDICAL INFRASTRUCTURE CO",
    "LABGEAR INTERNATIONAL", "Labgear Lab", "Lynor Argon S", "LYNOR HEALTHCARE", "Manish Medi Catalogue",
    "Manish Medi Innovation Pvt Ltd", "Maxxon Medical", "Medicaid Systems (Advance Medicaid) Catalogue", "MEDIKOP",
    "Meditrax Equipments", "Medsky (A-Sky)", "Medwin India", "MONARCPLUS MANUFACTURERS PRIVATE LIMITED",
    "OPHTHALMIC SOLUTION", "Opxica Industries", "Osm dudes Catalogue", "Paramount Furniture",
    "Paramount Hospital Furniture & Equipments", "Paras Enterprises", "Polar Automation", "RAI MEDICAL EQUIPMENTS",
    "Rastogi Catalogue", "RASTOGI STEEL FURNITURE", "Reliance Instruments", "Resuscitations", "Rohilla Industries Furniture",
    "Safe & Fresh Products", "Sai Lab Catalogue", "SAILAB EQUIPMENT", "SARVAGYA", "Shree Gopinath (SMK)",
    "Shri Gurukrupa", "Smk Meditech Disposable", "Sparsh Solutions", "SS HOSPITAL CARE", "Sumit Surgical",
    "Sumit Surgicals", "Sunfox", "Supreme Surgical (Patel)", "TECHNOCARE MEDISYSTEMS", "TNT Physio", "TRI-X",
    "UNITECH VISION", "VAMIKA SALES AGENCIES", "Ventibeats Critical Care Catalogue", "VENTIBEATS MEDICAL LLP",
    "Walnut Catalogue", "WALNUT MEDICAL", "Zetrica Laser",
)
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt"}
GENERIC_FOLDERS = {
    "document", "documents", "doc", "docs", "file", "files", "upload", "uploads",
    "vendor", "vendors", "vender", "venders", "supplier", "suppliers", "attachment", "attachments",
    "kyc", "certificates", "certificate", "compliance", "company documents", "vendor documents",
    "all documents", "records", "data", "handover", "handover files", "batch", "archive",
    "new folder", "scans", "scanned documents", "bank", "bank details", "statutory documents",
    "identity", "registration", "certifications", "current", "old", "new", "financial documents",
}
LEGAL_SUFFIX = re.compile(r"\b(?:pvt|private|limited|ltd|llp|inc|corporation|industries|enterprises|company)\b", re.I)
# Do not use substring matching for PAN/MD: 'company' and 'Spandan' are not PAN evidence.
PATTERNS = {
    "Cancelled Cheque": r"\bcheques?\b|\bchecks?\b|\bchq\b|\bcancel(?:led|ed)?\s*(?:cheq(?:ue)?|check)\b",
    "GST": r"\bgst(?:in|n)?\b|\bgoods\s+and\s+services\s+tax\b",
    "MD": r"(?<![a-z])md\s*[-_ ]*\s*\d{1,2}(?!\d)|\bmd\b(?!\s*[-_ ]*\d{3})|licen[cs]e\s*md\s*\d{1,2}(?!\d)|\bmdr\s+(?:licen[cs]e|registration)\b|\bmedical\s+device\s+licen[cs]e\b",
    "Udyam": r"\b(?:udyam|udhyam|udyog|udhyog|msme)\b|\budyamregistration\b",
    "ISO": r"\biso(?:\s*\d+(?!\d)|\b)|\b(?:9001|13485|14001|45001)\b",
    "PAN Card": r"\bpan\b|pan\s*card\b|\bpermanent\s+account\s+number\b",
    "Form 16": r"\bform\s*[-_ ]*16\b(?!\s*a\b)",
    "Aadhar": r"\b(?:aadhar|aadhaar|adhaar|aad har|adhar)\b|(?:aadhar|aadhaar|adhaar|adhar)\s*card\b",
    "Price": r"\b(?:price|pricing|pricelist|price\s*list|rate\s*list|rate\s*card)\b",
    "Catalogue": r"\b(?:catalogue|catalog|product\s+catalog(?:ue)?)\b",
}
SUPPORTING_ONLY = re.compile(
    r"\b(?:incorporation|startup|company\s+registration|ce|fda|lut|challan|invoice|brochure|quotation)\b"
    r"|certificateofincorporation|\backnowledgement\b", re.I
)


def normalize(value: str) -> str:
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", str(value)).casefold().replace("_", " ")).strip()


def clean_company(value: str) -> str:
    # Remove list numbering, not digits that are part of a company's real name.
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = re.sub(r"^\s*(?:\(?\d+\)?\s*[.)_-]\s*|\d+\s+)", "", value)
    return re.sub(r"\s+", " ", value).strip(" _-")


def company_key(value: str) -> str:
    return normalize(clean_company(value))


def path_parts(path: str) -> tuple[str, ...]:
    path = str(path).replace("\\", "/")
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts or any(":" in part or "\x00" in part for part in p.parts):
        raise ValueError("Unsafe file path in archive")
    return tuple(part for part in p.parts if part not in ("", "."))


def file_basename(path: str) -> str:
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def file_stem(path: str) -> str:
    name = file_basename(path)
    while PurePosixPath(name).suffix.lower() in ALLOWED_EXTENSIONS:
        name = name[: -len(PurePosixPath(name).suffix)]
    return unicodedata.normalize("NFKC", name).replace("_", " ")


def is_wrapper(folder: str) -> bool:
    value = normalize(clean_company(folder))
    if not value or value in GENERIC_FOLDERS or value.isdigit():
        return True
    if re.fullmatch(r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?", value):
        return True
    if re.fullmatch(r"(?:section|set|group|part|batch|lot|folder|annexure)(?:\s+[a-z0-9]+){0,2}", value):
        return True
    if LEGAL_SUFFIX.search(value):
        return False
    # Generic handover names, not a list of particular people.
    if re.search(r"\b(?:vendors?|venders?|suppliers?|handover)\b", value):
        return True
    return bool(re.search(r"\b(?:documents|docs|files|attachments)\s*$", value))


def is_type_folder(folder: str) -> bool:
    text = normalize(clean_company(folder))
    stripped = re.sub(r"\b(?:copy|copies|certificate|certificates|document|documents|registration|licence|license|front|back|card)\b", " ", text).strip()
    for pattern in PATTERNS.values():
        if re.fullmatch(pattern, stripped, re.I):
            return True
    return text in {"pan card", "form 16", "cancelled cheque", "cancel cheque", "asf iso", "iso", "md license", "md licence", "udyog aadhar", "udyog aadhaar"}


def types_from_name(name: str) -> list[str]:
    text = file_stem(name).lower()
    # GST invoices/challans and incorporation certificates do not prove that the
    # requested registration or PAN card itself was supplied.
    if SUPPORTING_ONLY.search(text):
        # An explicitly combined ISO+CE file is still ISO; a company PAN file is
        # valid, but a 'company registration certificate' is not a PAN file.
        if re.search(PATTERNS["ISO"], text, re.I):
            return ["ISO"]
        return []
    found = [t for t, pattern in PATTERNS.items() if re.search(pattern, text, re.I)]
    if "Udyam" in found and "Aadhar" in found:
        found.remove("Aadhar")  # Udyog Aadhaar is a business registration.
    return found


def normalize_document_types(types: Iterable[str]) -> list[str]:
    """Normalize category names retained by older dashboard versions."""
    return list(dict.fromkeys(LEGACY_DOCUMENT_TYPE_NAMES.get(str(value), str(value)) for value in types))


@dataclass(frozen=True)
class Decision:
    company_name: str | None
    document_types: tuple[str, ...]
    method: str
    reason: str
    handover: str = ""

    @property
    def needs_review(self) -> bool:
        return self.company_name is None or (not self.document_types and self.method != "Supporting document")


LEGAL_WORDS = {
    "pvt": "private", "pvtltd": "private limited", "ltd": "limited",
    "llc": "limited", "inc": "incorporated", "corp": "corporation",
    "co": "company",
}


def company_core_words(value: str) -> tuple[str, ...]:
    words = normalize(value).split()
    expanded = []
    for word in words:
        expanded.extend(LEGAL_WORDS.get(word, word).split())
    while expanded and expanded[-1] in {"private", "limited", "incorporated", "corporation", "company"}:
        expanded.pop()
    return tuple(expanded[:6])


def known_company_matches(candidate: str, known_names: Iterable[str]) -> list[str]:
    """Return canonical names that match a candidate without fuzzy merging."""
    candidate_key = company_key(candidate)
    if isinstance(known_names, dict):
        known = {company_key(alias): clean_company(canonical) for alias, canonical in known_names.items() if company_key(alias)}
    else:
        known = {company_key(name): clean_company(name) for name in known_names if company_key(name)}
    if candidate_key in known:
        return [known[candidate_key]]
    candidate_words = company_core_words(candidate)
    matches = []
    for key, name in known.items():
        known_words = company_core_words(name)
        shared = 0
        for left, right in zip(candidate_words, known_words):
            if left != right:
                break
            shared += 1
        if shared and (len(candidate_words) == 1 or len(known_words) == 1 or shared >= 2):
            matches.append(name)
    return sorted(set(matches), key=str.casefold)


def match_known_company(candidate: str, known_names: Iterable[str]) -> str | None:
    """Resolve a unique case-insensitive shared-initial company alias."""
    matches = known_company_matches(candidate, known_names)
    return matches[0] if len(matches) == 1 else None


def infer_company(path: str, known_names: Iterable[str] = (), forced_company: str = "") -> tuple[str | None, str, str]:
    if forced_company.strip():
        return clean_company(forced_company), "Selected company", ""
    parts = path_parts(path)
    parents = parts[:-1]
    wrappers: list[str] = []
    if isinstance(known_names, dict):
        known = {company_key(alias): clean_company(canonical) for alias, canonical in known_names.items() if company_key(alias)}
    else:
        known = {company_key(x): x for x in known_names if company_key(x)}
    for folder in parents:
        if is_wrapper(folder):
            wrappers.append(folder)
            continue
        if is_type_folder(folder):
            continue
        candidate = clean_company(folder)
        if candidate and not candidate.isdigit():
            matches = known_company_matches(candidate, known.values())
            if len(matches) == 1:
                return matches[0], "Company folder matched to canonical name", " / ".join(wrappers)
            if len(matches) > 1:
                return None, "Ambiguous company alias", " / ".join(wrappers)
            return candidate, "Company folder", " / ".join(wrappers)
    # Flat uploads: a known vendor may be matched by a complete name, never a
    # personal suffix such as 'GST - Mr Someone'. More than one match is unsafe.
    stem = file_stem(path)
    normalized = " " + normalize(stem) + " "
    matches = [(key, name) for key, name in known.items() if len(key) >= 3 and f" {key} " in normalized]
    if matches:
        matches.sort(key=lambda x: len(x[0]), reverse=True)
        best = matches[0]
        if len(matches) == 1 or all(key in best[0] for key, _ in matches[1:]):
            return best[1], "Vendor master + filename", " / ".join(wrappers)
        return None, "Ambiguous filename", " / ".join(wrappers)
    # A clear company-prefix / document-suffix is allowed. Do not derive company
    # names from a suffix after the document keyword; that can be a person's name.
    hits = [m for pattern in PATTERNS.values() if (m := re.search(pattern, stem, re.I))]
    if hits:
        first = min(hits, key=lambda m: m.start())
        prefix = clean_company(stem[:first.start()])
        if len(prefix) >= 3 and not is_wrapper(prefix) and not prefix.isdigit() and normalize(prefix) not in {
            "copy", "masked", "mask", "director", "certificate", "new", "latest", "signed", "cancel", "cancelled", "canceled", "form"
        }:
            return prefix, "Company prefix in filename", " / ".join(wrappers)
    return None, "Needs company review", " / ".join(wrappers)


def classify(path: str, known_names: Iterable[str] = (), content: bytes | None = None, forced_company: str = "", read_pdf_text: bool = False) -> Decision:
    company, ownership, handover = infer_company(path, known_names, forced_company)
    stem = file_stem(path)
    # Remove the company label from category detection (e.g. 'PAN Industries').
    if company:
        stem = re.sub(re.escape(company).replace(r"\ ", r"[ _]+"), " ", stem, flags=re.I)
    types = types_from_name(stem)
    method = "Filename"
    supporting = bool(SUPPORTING_ONLY.search(stem))
    if not types and not supporting:
        for folder in reversed(path_parts(path)[:-1]):
            if is_type_folder(folder):
                types = types_from_name(folder)
                if types:
                    method = "Document-type folder"
                    break
    reason = f"{ownership}; {method.lower()} labels. Presence only, not document validity."
    if not types and content and read_pdf_text and path.lower().endswith(".pdf") and not supporting:
        types = types_from_pdf(content)
        if types:
            method = "PDF heading"
            reason = f"{ownership}; readable PDF heading. Presence only, not validity."
    if not types:
        method = "Needs review"
        reason = "Other/supporting document; not counted as a checklist certificate." if supporting else "Document type unclear. File retained; review classification. Scanned content is not OCR-verified."
    if not types and supporting_category(stem):
        method = "Supporting document"
        reason = f"Recognized {supporting_category(stem)}. Retained outside the checklist categories; validity not checked."
    if not company:
        reason = "Company unclear. Put this file inside its company folder, or assign it in Review queue. " + reason
    return Decision(company, tuple(types), method, reason, handover)


def types_from_pdf(content: bytes) -> list[str]:
    """Conservative text-only fallback. No OCR; no broad body-text substrings."""
    if len(content) > 12 * 1024 * 1024:
        return []
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            return []
        headings = "\n".join((page.extract_text() or "")[:4000] for page in reader.pages[:2])
        text = normalize(headings)
    except Exception:
        return []
    if not text:
        return []
    rules = {
        "GST": [r"form gst reg 06", r"goods and services tax.*certificate of registration"],
        "Udyam": [r"udyam registration certificate", r"udyog aadhar.*memorandum", r"udyog aadhaar.*memorandum"],
        "ISO": [r"(?:iso|iso iec) (?:9001|13485|14001|45001).*(?:certif|management system)", r"certif.*(?:iso|iso iec) (?:9001|13485|14001|45001)"],
        "MD": [r"form md (?:5|6|9|10|13|15|42)\b.*(?:licence|license|permission|registration)", r"licen[cs]e.*manufactur.*medical devices"],
        "PAN Card": [r"income tax department.*(?:permanent account number card|permanent account number).*government of india", r"income tax department.*government of india.*permanent account number"],
        "Form 16": [r"form (?:no )?16\b.*certificate under section 203"],
        "Aadhar": [r"unique identification authority of india.*(?:date of birth|dob|year of birth)", r"government of india.*(?:date of birth|dob).*mera aadhaar"],
    }
    found = [t for t, patterns in rules.items() if any(re.search(p, text, re.I) for p in patterns)]
    # More than one certificate heading in otherwise unnamed content is ambiguous.
    return found if len(found) == 1 else []


@dataclass
class ArchiveLimits:
    max_entries: int = 5000
    max_total_bytes: int = 5 * 1024 * 1024 * 1024
    max_file_bytes: int = 128 * 1024 * 1024
    max_nested_bytes: int = 256 * 1024 * 1024
    max_depth: int = 2
    entries: int = 0
    total_bytes: int = 0
    skipped: list[dict] = field(default_factory=list)


def iter_archive(content: bytes, source: str, limits: ArchiveLimits, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, bytes, str]]:
    if depth > limits.max_depth:
        raise ValueError("ZIP nesting limit exceeded. Upload a company-folder ZIP instead.")
    with zipfile.ZipFile(BytesIO(content) if isinstance(content, bytes) else content) as archive:
        for member in sorted(archive.infolist(), key=lambda m: natural_key(m.filename)):
            if member.is_dir():
                continue
            limits.entries += 1
            if limits.entries > limits.max_entries:
                raise ValueError(f"More than {limits.max_entries:,} entries. Split the upload into smaller batches.")
            try:
                parts = path_parts(member.filename)
            except ValueError:
                limits.skipped.append({"File": member.filename, "Reason": "Unsafe path"}); continue
            if not parts or any(part.startswith((".", "__MACOSX")) for part in parts) or parts[-1].lower() in {"thumbs.db", "desktop.ini"}:
                continue
            if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                limits.skipped.append({"File": member.filename, "Reason": "Symlink/encrypted ZIP entry"}); continue
            ext = PurePosixPath(parts[-1]).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS and ext != ".zip":
                limits.skipped.append({"File": member.filename, "Reason": "Unsupported file type"}); continue
            bound = limits.max_nested_bytes if ext == ".zip" else limits.max_file_bytes
            if member.file_size > bound or (member.compress_size > 0 and member.file_size / member.compress_size > 1000):
                limits.skipped.append({"File": member.filename, "Reason": "File too large / unsafe compression ratio"}); continue
            limits.total_bytes += member.file_size
            if limits.total_bytes > limits.max_total_bytes:
                raise ValueError("Expanded ZIP exceeds the configured batch limit. Split the upload into smaller batches.")
            full_path = "/".join(filter(None, (prefix, "/".join(parts))))
            try:
                if ext == ".zip":
                    import shutil
                    import tempfile
                    with tempfile.TemporaryFile() as nested:
                        with archive.open(member) as file:
                            shutil.copyfileobj(file, nested, length=1024 * 1024)
                        nested.seek(0)
                        yield from iter_archive(nested, source, limits, full_path[:-4], depth + 1)
                    continue
                with archive.open(member) as file:
                    payload = file.read(bound + 1)
                if len(payload) > bound or not payload:
                    limits.skipped.append({"File": full_path, "Reason": "Empty / oversized file"}); continue
                yield full_path, payload, source
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                limits.skipped.append({"File": full_path, "Reason": f"Unreadable archive entry ({type(exc).__name__})"})


def upload_stream(upload):
    """Use seekable uploads directly; support legacy byte-only callers."""
    if hasattr(upload, "read") and hasattr(upload, "seek"):
        upload.seek(0)
        return upload
    return BytesIO(upload.getvalue())


def iter_uploads(uploads, limits: ArchiveLimits | None = None) -> Iterator[tuple[str, bytes, str]]:
    limits = limits or ArchiveLimits()
    for upload in uploads:
        source = file_basename(upload.name)
        if source.lower().endswith(".zip"):
            yield from iter_archive(upload_stream(upload), source, limits)
        elif PurePosixPath(source).suffix.lower() in ALLOWED_EXTENSIONS:
            data = upload_stream(upload).read(limits.max_file_bytes + 1)
            limits.entries += 1
            limits.total_bytes += len(data)
            if limits.entries > limits.max_entries or limits.total_bytes > limits.max_total_bytes:
                raise ValueError("Upload batch limit exceeded. Split the files into smaller batches.")
            if data and len(data) <= limits.max_file_bytes:
                yield source, data, source
            else:
                limits.skipped.append({"File": source, "Reason": "Empty file / over 128 MB"})
        else:
            limits.skipped.append({"File": source, "Reason": "Unsupported file type"})


def natural_key(value: str) -> list:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def read_vendor_file(upload) -> pd.DataFrame:
    frames = [pd.read_csv(BytesIO(upload.getvalue()), dtype=str)] if upload.name.lower().endswith(".csv") else list(pd.read_excel(BytesIO(upload.getvalue()), sheet_name=None, dtype=str).values())
    names: list[str] = []
    for frame in frames:
        column = next((c for c in frame.columns if normalize(c) in {"company", "company name", "companyname", "vendor", "vendor name", "vendorname", "name", "supplier", "supplier name"}), None)
        if column is not None:
            names.extend(clean_company(x) for x in frame[column].dropna().astype(str) if str(x).strip())
    if not names:
        raise ValueError("No company names found. Use a Company Name / Vendor Name column.")
    return pd.DataFrame({"company_name": names}).assign(company_key=lambda x: x.company_name.map(company_key)).drop_duplicates("company_key")


def build_checklist(vendors: pd.DataFrame, documents: pd.DataFrame) -> pd.DataFrame:
    id_column = ["canonical_id"] if "canonical_id" in vendors.columns else []
    columns = ["company_key", *id_column, "Company Name", *DOCUMENT_TYPES, "Available", "Missing", "Completion", "Files", "Needs review"]
    if vendors.empty:
        return pd.DataFrame(columns=columns)
    grouped = {key: group for key, group in documents.groupby("company_key")} if not documents.empty else {}
    rows = []
    for vendor in vendors.itertuples(index=False):
        group = grouped.get(vendor.company_key)
        present: set[str] = set()
        review = 0
        if group is not None:
            for document in group.itertuples(index=False):
                if document.available:
                    present.update(document.types)
                if document.needs_review or not document.available:
                    review += 1
        found = len(present.intersection(DOCUMENT_TYPES))
        row = {"company_key": vendor.company_key, "Company Name": vendor.company_name,
                     **{t: "Yes" if t in present else "No" for t in DOCUMENT_TYPES},
                     "Available": found, "Missing": len(DOCUMENT_TYPES) - found, "Completion": found / len(DOCUMENT_TYPES),
                     "Files": len(group) if group is not None else 0, "Needs review": review}
        if "canonical_id" in vendors.columns:
            row["canonical_id"] = vendor.canonical_id
        rows.append(row)
    return pd.DataFrame(rows, columns=columns).sort_values("Company Name", key=lambda s: s.str.casefold()).reset_index(drop=True)


def filter_checklist(checklist: pd.DataFrame, search: str = "", status: str = "All companies") -> pd.DataFrame:
    frame = checklist
    if search.strip():
        frame = frame[frame["Company Name"].str.contains(search.strip(), case=False, regex=False, na=False)]
    if status == "Missing documents":
        frame = frame[frame["Missing"] > 0]
    elif status == "All types available":
        frame = frame[frame["Missing"] == 0]
    elif status == "Needs review":
        frame = frame[frame["Needs review"] > 0]
    return frame.reset_index(drop=True)


def discover_folder_companies(uploads) -> list[str]:
    """Include explicit empty company folders as all-No checklist rows."""
    names = {}
    for upload in uploads:
        if not upload.name.lower().endswith(".zip"):
            continue
        try:
            with zipfile.ZipFile(upload_stream(upload)) as archive:
                members = archive.infolist()
                if len(members) > 5000:
                    continue
                for member in members:
                    try:
                        parts = path_parts(member.filename)
                    except ValueError:
                        continue
                    if not parts or any(p.startswith((".", "__MACOSX")) for p in parts):
                        continue
                    path = "/".join(parts) + ("/unclassified-placeholder" if member.is_dir() else "")
                    name, method, _ = infer_company(path)
                    if name and method == "Company folder":
                        names[company_key(name)] = name
        except zipfile.BadZipFile:
            continue
    return sorted(names.values(), key=str.casefold)


SUPPORTING_TYPES = {
    "Incorporation": r"incorporation|fillip.*approval|company[ _-]*registration|\bmca\b|coi$",
    "IEC": r"\biec\b|import.*export.*code",
    "CE": r"\bce\b",
    "FDA": r"\bfda\b",
    "Trademark": r"trade[ _-]*mark",
    "Bank details": r"\bbank\b.*(?:detail|letter|certificate)",
    "Brochure": r"brochure|catalogue|catalog",
    "Invoice / challan": r"invoice|challan",
    "Factory licence": r"factory.*licen[cs]e",
    "Drug / regulatory licence": r"\bcdsco\b|drug.*licen[cs]e|\bslr\b|\bclr\b",
    "Startup registration": r"start+up|dpiit",
    "Quality certificate": r"quality[ _-]*management|\bqms\b|\bgmp\b|\bbscic\b|dakks|\bnabl\b|iec\s*60601|\bqc\b",
    "Compliance certificate": r"compli[ae]nce|complience",
    "Other registration": r"registration|\bnsic\b|\bvat\b|\bdic\b|shop[ _-]*act|enlistment",
    "LUT acknowledgement": r"\blut\b",
    "Passport": r"passport",
    "Marketplace certificate": r"india[ _-]*mart",
    "EC certificate": r"\bec\b.*certif",

}


def supporting_category(name: str) -> str:
    text = file_stem(name).lower()
    return next((label for label, pattern in SUPPORTING_TYPES.items() if re.search(pattern, text, re.I)), "")


def export_filename(company: str, suffix: str, extension: str) -> str:
    """Company-named downloads safe on Windows, macOS and browsers."""
    name = unicodedata.normalize("NFKC", company)
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name)
    name = re.sub(r"\s+", "_", name).strip(" ._")[:100] or "Company"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", name, re.I):
        name = "Company_" + name
    return f"{name}_{suffix}.{extension.lstrip('.')}"


def dashboard_counts(vendors: pd.DataFrame, documents: pd.DataFrame) -> dict:
    """Global totals never change when a screen filter is applied."""
    checklist = build_checklist(vendors, documents)
    vendor_keys = set(vendors.company_key.dropna().astype(str)) if "company_key" in vendors else set()
    document_keys = set(documents.loc[documents.company_key.notna() & documents.company_key.ne(""), "company_key"].astype(str)) if "company_key" in documents else set()
    return {
        "companies": len(vendor_keys | document_keys),
        "stored_files": int(documents.available.sum()),
        "review_files": int((documents.needs_review | ~documents.available).sum()),
        "complete_companies": int((checklist.Missing == 0).sum()),
        "unassigned_files": int((documents.company_key == "").sum()),
    }
