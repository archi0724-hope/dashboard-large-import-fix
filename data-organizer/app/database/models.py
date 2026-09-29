"""Database schema.

DuckDB is used because it is embedded (no server to install), columnar (fast aggregates over
millions of rows) and can write CSV/Parquet/JSON natively.

Layering mirrors the pipeline::

    source_files      inventory of files discovered in the source folder (read-only inputs)
    raw_records       RAW/STAGING layer - values exactly as extracted, one row per source row
    cleaned_records   normalised values + validation flags (never overwrites raw_records)
    quality_issues    one row per data-quality finding (record level)
    resolutions       record -> master entity decision, with score / method / reason / evidence
    master_entities   the canonical (standardised, de-duplicated) entity table
    aliases           every distinct name variant per master entity (derived, plus verification)
    review_items      uncertain matches waiting for a human decision
    user_decisions    persistent "learning" table: verified alias mappings and rejected pairs
    duplicate_groups  exact / near duplicate groups (nothing is deleted)
    processing_errors every extraction/processing error (file, page/sheet/row, error, time)
"""

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS review_seq START 1;
CREATE SEQUENCE IF NOT EXISTS error_seq START 1;
CREATE SEQUENCE IF NOT EXISTS run_seq START 1;
CREATE SEQUENCE IF NOT EXISTS raw_seq START 1;

CREATE TABLE IF NOT EXISTS source_files (
    file_key          VARCHAR PRIMARY KEY,
    source_kind       VARCHAR,
    file_name         VARCHAR,
    source_path       VARCHAR,
    mime_type         VARCHAR,
    ext               VARCHAR,
    size_bytes        BIGINT,
    modified_time     VARCHAR,
    fingerprint       VARCHAR,
    local_path        VARCHAR,
    status            VARCHAR,
    records_extracted BIGINT DEFAULT 0,
    sheets            INTEGER,
    pages             INTEGER,
    notes             VARCHAR,
    error             VARCHAR,
    inspection        VARCHAR,
    discovered_at     TIMESTAMP,
    processed_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_records (
    seq                       BIGINT DEFAULT nextval('raw_seq'),
    record_id                 VARCHAR,
    file_key                  VARCHAR,
    file_fingerprint          VARCHAR,
    is_current                BOOLEAN DEFAULT TRUE,
    source_file               VARCHAR,
    source_path               VARCHAR,
    source_type               VARCHAR,
    source_sheet              VARCHAR,
    source_page               INTEGER,
    source_row                BIGINT,
    original_name             VARCHAR,
    original_address          VARCHAR,
    original_city             VARCHAR,
    original_district         VARCHAR,
    original_state            VARCHAR,
    original_pincode          VARCHAR,
    original_phone            VARCHAR,
    original_email            VARCHAR,
    original_website          VARCHAR,
    original_registration_id  VARCHAR,
    original_category         VARCHAR,
    original_data             VARCHAR,
    column_map                VARCHAR,
    extraction_method         VARCHAR,
    extracted_at              TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cleaned_records (
    record_id             VARCHAR,
    name_normalized       VARCHAR,
    name_core             VARCHAR,
    name_display          VARCHAR,
    place_hint            VARCHAR,
    name_variants         VARCHAR,
    acronym_key           VARCHAR,
    city_norm             VARCHAR,
    city_display          VARCHAR,
    district_norm         VARCHAR,
    district_display      VARCHAR,
    state_norm            VARCHAR,
    state_display         VARCHAR,
    pincode               VARCHAR,
    address_norm          VARCHAR,
    phone_norm            VARCHAR,
    phones_all            VARCHAR,
    email_norm            VARCHAR,
    emails_all            VARCHAR,
    website_norm          VARCHAR,
    website_domain        VARCHAR,
    registration_id_norm  VARCHAR,
    registration_type     VARCHAR,
    category_norm         VARCHAR,
    entity_type_hint      VARCHAR,
    source_year           INTEGER,
    evidence_strength     INTEGER,
    quality_severity      VARCHAR,
    quality_flags         VARCHAR,
    is_resolvable         BOOLEAN,
    cleaned_at            TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quality_issues (
    record_id    VARCHAR,
    master_entity_id VARCHAR,
    issue_type   VARCHAR,
    severity     VARCHAR,
    field        VARCHAR,
    value        VARCHAR,
    message      VARCHAR,
    detected_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS master_entities (
    master_entity_id    VARCHAR PRIMARY KEY,
    standard_name       VARCHAR,
    name_normalized     VARCHAR,
    aliases             VARCHAR,
    city                VARCHAR,
    district            VARCHAR,
    state               VARCHAR,
    address             VARCHAR,
    pincode             VARCHAR,
    phone               VARCHAR,
    email               VARCHAR,
    website             VARCHAR,
    registration_id     VARCHAR,
    all_phones          VARCHAR,
    all_emails          VARCHAR,
    all_websites        VARCHAR,
    entity_type         VARCHAR,
    category            VARCHAR,
    confidence          DOUBLE,
    verification_status VARCHAR,
    record_count        INTEGER DEFAULT 0,
    source_file_count   INTEGER DEFAULT 0,
    evidence_strength   INTEGER DEFAULT 0,
    data_conflicts      VARCHAR,
    state_json          VARCHAR,
    merged_into         VARCHAR,
    created_at          TIMESTAMP,
    updated_at          TIMESTAMP
);

CREATE TABLE IF NOT EXISTS resolutions (
    record_id         VARCHAR,
    master_entity_id  VARCHAR,
    match_score       DOUBLE,
    match_band        VARCHAR,
    match_method      VARCHAR,
    match_status      VARCHAR,
    reason            VARCHAR,
    evidence          VARCHAR,
    review_id         INTEGER,
    verified_by_user  BOOLEAN DEFAULT FALSE,
    resolved_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS aliases (
    alias              VARCHAR,
    alias_normalized   VARCHAR,
    master_entity_id   VARCHAR,
    original_source    VARCHAR,
    match_score        DOUBLE,
    match_method       VARCHAR,
    occurrences        INTEGER,
    verified           BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS review_items (
    review_id            INTEGER PRIMARY KEY DEFAULT nextval('review_seq'),
    alias_normalized     VARCHAR,
    original_name        VARCHAR,
    context_city         VARCHAR,
    provisional_master_id VARCHAR,
    candidate_master_id  VARCHAR,
    match_score          DOUBLE,
    match_band           VARCHAR,
    reason               VARCHAR,
    evidence             VARCHAR,
    ai_verdict           VARCHAR,
    record_count         INTEGER DEFAULT 1,
    sample_record_id     VARCHAR,
    source_file          VARCHAR,
    source_row           BIGINT,
    source_page          INTEGER,
    status               VARCHAR DEFAULT 'pending',
    decided_at           TIMESTAMP,
    decided_by           VARCHAR,
    created_at           TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_decisions (
    alias_normalized  VARCHAR,
    context_city      VARCHAR,
    master_entity_id  VARCHAR,
    decision          VARCHAR,          -- 'map' (verified alias -> master) | 'reject' (alias is NOT this master)
    verified_by_user  BOOLEAN DEFAULT TRUE,
    review_id         INTEGER,
    decided_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    group_id            VARCHAR,
    dup_type            VARCHAR,
    dup_key             VARCHAR,
    record_id           VARCHAR,
    master_entity_id    VARCHAR,
    similarity          DOUBLE,
    is_suggested_primary BOOLEAN,
    detected_at         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS processing_errors (
    error_id     BIGINT DEFAULT nextval('error_seq'),
    file_key     VARCHAR,
    file_name    VARCHAR,
    location     VARCHAR,
    stage        VARCHAR,
    severity     VARCHAR,
    error_type   VARCHAR,
    message      VARCHAR,
    traceback    VARCHAR,
    occurred_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id       BIGINT DEFAULT nextval('run_seq'),
    stage        VARCHAR,
    status       VARCHAR,
    total        BIGINT,
    done         BIGINT,
    message      VARCHAR,
    started_at   TIMESTAMP,
    updated_at   TIMESTAMP,
    finished_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kv_state (
    key         VARCHAR PRIMARY KEY,
    value       VARCHAR,
    updated_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key   VARCHAR PRIMARY KEY,
    response    VARCHAR,
    created_at  TIMESTAMP
);
"""

# Tables that are derived and safe to rebuild; raw_records / source_files / user_decisions are never wiped
# by a normal re-run (source data and human decisions are precious).
DERIVED_TABLES = (
    "cleaned_records", "quality_issues", "resolutions", "aliases",
    "master_entities", "review_items", "duplicate_groups",
)

INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_master_merged ON master_entities(merged_into)",
]
