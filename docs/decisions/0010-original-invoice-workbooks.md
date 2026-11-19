# 0010: Preserve original served-data invoice workbooks

Status: accepted for the requested worksheet-style Purchase Invoice page.

The user requested the original workbook workflow: worksheet selection, search
across all cells, original row/column order, row counts, and the original Excel
download. Extracting a workbook into the existing invoice fields cannot preserve
its multiple worksheets, additional columns, formatting, or duplicate rows.

Store each validated original XLSX as immutable, content-addressed bytes in a
separate `vdd_invoice_workbooks` table, using schema version 1. Keep the filename,
worksheet dimensions and creation time alongside the bytes. Repeated content is
idempotent; changed content creates another version. Do not insert workbook rows
into extracted invoices, classify supplier/client fields, or change existing
invoice/document counts. Keep the invoice review and 31-column export below the
original-workbook view.

The existing SQLite/PostgreSQL connection abstraction creates the additive table
idempotently. No new dependency or migration framework is introduced. Tests cover
reopened SQLite connections and exact original downloads; the same DDL and BYTEA
storage are verified with synthetic bytes on the existing Neon validation branch.
Production workbook import happens through the authenticated upload control;
private client workbooks never belong in Git or public static assets.

Rollback means running the prior code while leaving the new table intact.
Existing invoice and vendor tables are not rewritten. Original downloads and
provider database backups are the recovery path for workbook versions. The
dashboard's existing backup/reset functions do not include this table. Keep the
local source file as an independent recovery copy.

Bound ZIP size, expanded size, entries, compression ratio, worksheet count,
dimensions and total cells before accepting a file. Search uses cached cell
values and literal text, preserves original order, and pages responses to avoid
shipping the complete workbook on each query. It does not evaluate formulas or
save spreadsheet edits. The download preserves the original bytes and formulas.
