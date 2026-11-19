"""Read shared Drive rows and original invoice worksheets without changing sources."""
from io import BytesIO
import json
import re

from openpyxl import load_workbook


def table_exists(store, db, table):
    if store.cloud:
        return bool(db.execute("SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?", (table,)).fetchone())
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def signature(store):
    values = []
    with store.connection() as db:
        for table, date_column in (("vdd_invoice_workbooks", "created_at"), ("vdd_drive_entries", "updated_at"), ("vdd_drive_records", "")):
            if table_exists(store, db, table):
                date = f", COALESCE(MAX({date_column}), '') AS latest" if date_column else ""
                row = db.execute(f"SELECT COUNT(*) AS n{date} FROM {table}").fetchone()
                values.append((int(row["n"]), str(row["latest"]) if date_column else ""))
            else:
                values.append((0, ""))
    return tuple(values)


def results(store, query):
    """Search every indexed Drive row and every saved worksheet, retaining source IDs."""
    from app.served_workbook import cell_text

    found, warnings = [], []
    ignored = {"show", "find", "search", "please", "give", "me", "the", "all", "what", "which", "is", "are", "of", "for", "in", "and", "does", "have", "has", "do", "sell", "sells"}
    terms = [token for token in re.findall(r"\w+", query.casefold()) if token not in ignored and len(token) > 1]
    if not terms:
        return found, warnings
    with store.connection() as db:
        if table_exists(store, db, "vdd_drive_records"):
            conditions = " AND ".join("search_text LIKE ? ESCAPE '!'" for _ in terms)
            patterns = tuple("%" + term.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%" for term in terms)
            for row in db.execute(f"SELECT record_key,record_json FROM vdd_drive_records WHERE {conditions}", patterns):
                record = json.loads(row["record_json"])
                found.append({"title": record.get("product") or record.get("company") or record.get("source_name", "Drive row"),
                    "source": "Drive library", "result_id": "drive:"+row["record_key"], "priority": 1, "score_hint": 12,
                    "text": json.dumps(record, ensure_ascii=False), "vendor": record.get("company", ""),
                    "product": record.get("product", ""), "source_file": record.get("member_path") or record.get("source_name", ""),
                    "source_url": record.get("source_url", ""), "review_status": record.get("review_status", "Needs review")})
        if table_exists(store, db, "vdd_invoice_workbooks"):
            workbooks = db.execute("SELECT id,filename FROM vdd_invoice_workbooks ORDER BY created_at DESC,id").fetchall()
        else:
            workbooks = []
    for row in workbooks:
        try:
            with store.connection() as db:
                original = db.execute("SELECT payload FROM vdd_invoice_workbooks WHERE id=?", (row["id"],)).fetchone()
            if original is None:
                continue
            book = load_workbook(BytesIO(bytes(original["payload"])), read_only=True, data_only=True, keep_links=False)
            try:
                for sheet in book.worksheets:
                    cells = sheet.iter_rows(values_only=True)
                    headers = [cell_text(value) for value in next(cells, ())]
                    for number, values in enumerate(cells, 2):
                        text = " ".join(cell_text(value) for value in values)
                        if not all(term in text.casefold() for term in terms):
                            continue
                        found.append({"title": f"{row['filename']} · {sheet.title} · row {number}",
                            "source": "Invoice worksheets", "result_id": f"workbook:{row['id']}:{sheet.title}:{number}",
                            "priority": 1, "score_hint": 12, "text": " ".join(headers)+" "+text,
                            "source_file": row["filename"], "source_sheet": sheet.title, "source_page": str(number),
                            "workbook_id": row["id"]})
            finally:
                book.close()
        except Exception:  # Isolate malformed saved files so other sources remain searchable.
            warnings.append(f"{row['filename']}: original workbook could not be searched.")
    return found, warnings
