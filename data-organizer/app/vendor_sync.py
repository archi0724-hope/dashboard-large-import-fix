"""Copy company-organized originals and extracted catalogue rows into Vendor."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pandas as pd

MAX_VENDOR_SYNC_FILE_BYTES = 128 * 1024 * 1024


def sync_organized_company_files(
    organized_folder: Path,
    store,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    """Idempotently add organized originals and extracted product rows to Vendor."""
    from catalogue_extraction import extract_records
    from vendor_core import classify

    root = Path(organized_folder).resolve()
    company_folders = [
        path
        for path in root.iterdir()
        if path.is_dir()
        and not path.is_symlink()
        and path.name.casefold() != "work related data"
    ]
    review_folder = root / "Work Related Data"
    source_folders = company_folders + (
        [review_folder]
        if review_folder.is_dir() and not review_folder.is_symlink()
        else []
    )
    files = sorted(
        (
            file
            for folder in source_folders
            for file in folder.rglob("*")
            if file.is_file()
            and not file.is_symlink()
            and file.resolve().is_relative_to(root)
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    result = {
        "documents_added": 0,
        "duplicate_documents": 0,
        "products_added": 0,
        "unassigned_documents_added": 0,
        "skipped": [],
        "company_folders": len(company_folders),
    }
    known_names = store.matching_companies()
    if company_folders:
        store.upsert_vendors(
            pd.DataFrame(
                {
                    "company_name": [folder.name for folder in company_folders],
                }
            )
        )
        known_names = store.matching_companies()

    for done, source in enumerate(files, 1):
        relative = source.relative_to(root)
        unassigned_folder = relative.parts[0].casefold() == "work related data"
        company = "" if unassigned_folder else relative.parts[0]
        label = relative.as_posix()
        if progress:
            progress(done, len(files), label)
        try:
            size = source.stat().st_size
            if size == 0:
                result["skipped"].append({"file": label, "reason": "Empty source file"})
                continue
            if size > MAX_VENDOR_SYNC_FILE_BYTES:
                result["skipped"].append(
                    {
                        "file": label,
                        "reason": "File exceeds Vendor's 128 MB per-document limit",
                    }
                )
                continue
            content = source.read_bytes()
        except OSError as error:
            result["skipped"].append(
                {
                    "file": label,
                    "reason": f"Could not read original file: {error}",
                }
            )
            continue

        decision = classify(
            source.name if unassigned_folder else label,
            known_names,
            content,
            forced_company=company,
        )
        added = store.save_document(
            label,
            content,
            decision,
            source_batch="Organizer company-folder sync",
            dedupe_across_companies=False,
        )
        result["documents_added"] += int(added)
        result["unassigned_documents_added"] += int(added and not decision.company_name)
        result["duplicate_documents"] += int(not added)
        document_id = store.document_id_for(
            label, content, decision.company_name or "", match_company=True
        )
        if document_id is None:
            result["skipped"].append(
                {
                    "file": label,
                    "reason": "Vendor stored no document record for this original",
                }
            )
            continue

        kind, records = extract_records(source.name, content)
        if kind:
            store.add_document_types(
                document_id, ["Price" if kind == "price" else "Catalogue"]
            )
        result["products_added"] += store.save_product_records(document_id, records)

    result["files_scanned"] = len(files)
    result["skipped_count"] = len(result["skipped"])
    return result
