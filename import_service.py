"""Testable ZIP import. Existing records are appended, never silently reset."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Callable
import pandas as pd
from vendor_core import ArchiveLimits, classify, discover_folder_companies, iter_uploads, company_key, clean_company, upload_stream

@dataclass
class ImportResult:
    source: str
    detected_companies: int = 0
    processed_files: int = 0
    saved_files: int = 0
    duplicate_files: int = 0
    review_files: int = 0
    total_companies: int = 0
    total_stored_files: int = 0
    issues: list[dict] = field(default_factory=list)
    company_names: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    upload_archives: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def import_documents(store, uploads, forced_company: str = "", read_pdf_text: bool = False,
                     progress: Callable | None = None, retain_archive: bool = True) -> ImportResult:
    uploads = list(uploads)
    result = ImportResult(source=", ".join(u.name for u in uploads))
    names = store.matching_companies()
    detected = set(discover_folder_companies(uploads)) if not forced_company.strip() else {forced_company.strip()}
    if detected:
        store.upsert_vendors(pd.DataFrame({"company_name": sorted(detected, key=str.casefold)}))
    for name in detected:
        names.setdefault(name, name)
    limits = ArchiveLimits()
    try:
        for upload in uploads:
            archive_detected = set(discover_folder_companies([upload])) if not forced_company.strip() else {forced_company.strip()}
            archive_processed = 0
            archive_issues_before = len(result.issues) + len(limits.skipped)
            try:
                for path, content, source in iter_uploads([upload], limits):
                    result.processed_files += 1
                    archive_processed += 1
                    if progress:
                        progress(result.processed_files, path)
                    try:
                        decision = classify(path, names, content, forced_company, read_pdf_text)
                        if decision.company_name is None:
                            store.add_review_item("", source=source, record=path, evidence=decision.reason)
                        added = store.save_document(path, content, decision, source)
                        result.saved_files += int(added)
                        result.duplicate_files += int(not added)
                        result.review_files += int(added and decision.needs_review)
                        if decision.company_name:
                            detected.add(decision.company_name)
                            archive_detected.add(decision.company_name)
                            if decision.company_name not in names:
                                names[decision.company_name] = decision.company_name
                    except Exception as error:
                        result.issues.append({"File": path, "Reason": f"Not saved: {type(error).__name__}. Check this file/storage and retry."})
            except Exception as error:
                result.issues.append({"File": upload.name, "Reason": f"Stopped: {str(error) if isinstance(error, ValueError) else type(error).__name__}. Saved records are retained; retry the upload."})
            if upload.name.lower().endswith(".zip"):
                stream = upload_stream(upload)
                stream.seek(0, 2)
                archive_size = stream.tell()
                stream.seek(0)
                # The existing archive store and download UI both materialize a blob.
                # Keep large originals at their source rather than allocating GBs.
                if not retain_archive or archive_size > 32 * 1024 * 1024:
                    result.notes.append(f"{upload.name}: original ZIP not copied to Uploaded ZIPs. Keep your source ZIP; extracted documents are saved separately.")
                    continue
                try:
                    issue_count = max(0, len(result.issues) + len(limits.skipped) - archive_issues_before)
                    archive_info = store.save_upload_archive(upload.name, stream.read(), sorted(archive_detected, key=str.casefold), archive_processed, issue_count)
                    result.upload_archives.append(archive_info)
                except Exception as error:
                    result.issues.append({"File": upload.name, "Reason": f"Original ZIP archive not saved: {type(error).__name__}. Documents already imported remain saved."})
    except Exception as error:
        result.issues.append({"File": "Batch", "Reason": f"Stopped: {str(error) if isinstance(error, ValueError) else type(error).__name__}. Saved records are retained; retry the upload."})
    result.issues.extend(limits.skipped)
    result.company_names = sorted({company_key(n): clean_company(n) for n in detected}.values(), key=str.casefold)
    result.detected_companies = len(result.company_names)
    saved_vendors = store.vendors()
    saved_documents = store.documents()
    vendor_keys = set(saved_vendors.company_key.dropna().astype(str))
    document_keys = set(saved_documents.loc[saved_documents.company_key.notna() & saved_documents.company_key.ne(""), "company_key"].astype(str))
    result.total_companies = len(vendor_keys | document_keys)
    result.total_stored_files = int(saved_documents.available.sum())
    store.log_event("Document upload", result.to_dict())
    return result

