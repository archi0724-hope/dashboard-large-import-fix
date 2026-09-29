import shutil

from app.config import RuntimeConfig
from app.database.repository import Repository
from app.matching.review import ReviewError, ReviewService
from app.cleaning.name_cleaner import NameCleaner
from app.pipeline import LocalSource, Pipeline, dry_run
import pytest


def q(repo, sql, *p):
    return repo.dicts(sql, list(p))


def test_full_run_shape(done_pipeline):
    r = done_pipeline.repo
    t = done_pipeline.totals()
    assert t["files"] == 11 and t["files_processed"] == 8 and t["files_failed"] == 1
    st = {x["status"]: x["n"] for x in q(r, "SELECT status, count(*) n FROM source_files GROUP BY 1")}
    assert st["unsupported"] == 1 and st["duplicate_file"] == 1 and st["failed"] == 1     # corrupt / md / exact copy: all reported, none fatal
    assert t["errors"] == 1                                                                 # only the corrupt workbook is an error
    ents = {x["standard_name"]: x for x in q(r, "SELECT * FROM master_entities WHERE merged_into IS NULL")}
    assert ents["Sawai Man Singh Hospital"]["record_count"] == 5
    assert ents["Aravali Care Hospital Private Limited"]["registration_id"].startswith("08AABCA1111A")
    assert ents["Eastern Star Hospital"]["record_count"] == 3 and ents["Western Star Hospital"]["record_count"] == 1


def test_no_record_is_lost(done_pipeline):
    r = done_pipeline.repo
    n_raw = r.scalar("SELECT count(*) FROM raw_records WHERE is_current")
    assert r.scalar("SELECT count(*) FROM resolutions") == n_raw == r.scalar("SELECT count(*) FROM cleaned_records")
    assert r.scalar("SELECT count(*) FROM raw_records WHERE original_data IS NULL OR original_data = ''") == 0


def test_every_record_is_traceable(done_pipeline):
    r = done_pipeline.repo
    bad = r.scalar("SELECT count(*) FROM raw_records WHERE source_file IS NULL OR source_row IS NULL")
    assert bad == 0 and r.scalar("SELECT count(*) FROM raw_records WHERE source_type = 'pdf' AND source_page IS NULL") == 0


def test_rerun_is_incremental_and_idempotent(settings, cfg, done_pipeline, demo_dir):
    r = done_pipeline.repo
    before = (r.scalar("SELECT count(*) FROM raw_records"), r.scalar("SELECT count(*) FROM master_entities"))
    p2 = Pipeline(settings, cfg, r, source=LocalSource(demo_dir))
    s = p2.run()
    assert s["extract"]["records"] == 0 and s["resolve"]["records"] == 0
    assert before == (r.scalar("SELECT count(*) FROM raw_records"), r.scalar("SELECT count(*) FROM master_entities"))


def test_new_file_is_processed_and_matched_against_existing(settings, cfg, done_pipeline, demo_dir, tmp_path):
    work = tmp_path / "in"
    shutil.copytree(demo_dir, work)
    r = Repository(":memory:")
    Pipeline(settings, RuntimeConfig(local_input_dir=str(work), output_dir=str(tmp_path / "o")), r, source=LocalSource(work)).run()
    n = r.scalar("SELECT count(*) FROM master_entities WHERE merged_into IS NULL")
    (work / "later.csv").write_text("Hospital Name,City,State,Phone\nSMS Hospital,Jaipur,Rajasthan,0141-2560101\nBrand New Hospital,Udaipur,Rajasthan,9876500999\n")
    p = Pipeline(settings, RuntimeConfig(local_input_dir=str(work), output_dir=str(tmp_path / "o")), r, source=LocalSource(work))
    s = p.run()
    assert s["extract"]["records"] == 2 and s["extract"]["files_skipped"] >= 8
    assert r.scalar("SELECT count(*) FROM master_entities WHERE merged_into IS NULL") == n + 1          # SMS joined the existing entity
    assert r.scalar("SELECT record_count FROM master_entities WHERE standard_name = 'Sawai Man Singh Hospital'") == 6
    r.close()


def test_stop_and_resume(settings, cfg, demo_dir):
    r = Repository(":memory:")
    p = Pipeline(settings, cfg, r, source=LocalSource(demo_dir))
    original = p._extract

    def extract_then_stop(*a, **k):
        out = original(*a, **k)
        p.progress.cancel.set()                               # user presses Stop while the run is in progress
        return out

    p._extract = extract_then_stop
    p.run()
    assert "Stopped" in p.progress.snapshot()["error"]
    assert r.scalar("SELECT count(*) FROM raw_records") == 53 and r.scalar("SELECT count(*) FROM master_entities") == 0
    p2 = Pipeline(settings, cfg, r, source=LocalSource(demo_dir))
    s = p2.run()                                               # resume: extraction is not repeated, the rest completes
    assert not p2.progress.snapshot()["error"] and s["extract"]["records"] == 0
    assert r.scalar("SELECT count(*) FROM master_entities") == 24
    r.close()


def test_review_accept_reject_new_and_learning(settings, cfg, demo_dir, tmp_path):
    r = Repository(":memory:")
    Pipeline(settings, cfg, r, source=LocalSource(demo_dir)).run()
    svc = ReviewService(r, NameCleaner("hospital"))
    items = svc.list_items()["items"]
    assert len(items) == 2
    by = {i["original_name"]: i for i in items}
    mary = by["Saint Mary Hospital Kota"]
    svc.decide(mary["review_id"], "accept")
    assert q(r, "SELECT record_count FROM master_entities WHERE master_entity_id = ?", mary["candidate_master_id"])[0]["record_count"] == 2
    assert r.scalar("SELECT count(*) FROM resolutions WHERE match_status = 'user_accepted'") == 1
    abc = by["ABC Hospital"]
    svc.decide(abc["review_id"], "new_entity", standard_name="ABC Hospital (unspecified)")
    assert r.scalar("SELECT count(*) FROM master_entities WHERE standard_name = 'ABC Hospital (unspecified)' AND verification_status = 'verified'") == 1
    with pytest.raises(ReviewError):
        svc.decide(mary["review_id"], "accept")               # already decided

    # learning: the same alias in a NEW file is applied automatically, no second review
    work = tmp_path / "in"
    shutil.copytree(demo_dir, work)
    (work / "more.csv").write_text("Hospital Name,City\nSaint Mary Hospital Kota,Kota\nABC Hospital,\n")
    s = Pipeline(settings, RuntimeConfig(local_input_dir=str(work), output_dir=str(tmp_path / "o")), r, source=LocalSource(work)).run()
    assert s["resolve"]["verified_mapping"] == 2 and r.scalar("SELECT count(*) FROM review_items WHERE status = 'pending'") == 0
    r.close()


def test_reject_is_remembered(settings, cfg, demo_dir, tmp_path):
    r = Repository(":memory:")
    Pipeline(settings, cfg, r, source=LocalSource(demo_dir)).run()
    svc = ReviewService(r, NameCleaner("hospital"))
    mary = [i for i in svc.list_items()["items"] if i["original_name"].startswith("Saint")][0]
    svc.decide(mary["review_id"], "reject")
    assert q(r, "SELECT count(*) n FROM user_decisions WHERE decision = 'reject'")[0]["n"] == 1
    r.close()


def test_merge_and_rename_entities(settings, cfg, done_pipeline):
    r = done_pipeline.repo
    svc = ReviewService(r, NameCleaner("hospital"))
    a = q(r, "SELECT master_entity_id FROM master_entities WHERE standard_name = 'Eastern Star Hospital'")[0]["master_entity_id"]
    b = q(r, "SELECT master_entity_id FROM master_entities WHERE standard_name = 'Western Star Hospital'")[0]["master_entity_id"]
    svc.merge_entities(b, a)
    assert r.scalar("SELECT record_count FROM master_entities WHERE master_entity_id = ?", [a]) == 4
    assert r.scalar("SELECT merged_into FROM master_entities WHERE master_entity_id = ?", [b]) == a
    svc.rename_entity(a, "Star Hospital (Jaipur)")
    assert r.scalar("SELECT standard_name FROM master_entities WHERE master_entity_id = ?", [a]) == "Star Hospital (Jaipur)"
    with pytest.raises(ReviewError):
        svc.merge_entities(a, a)


def test_duplicates_detected_not_deleted(done_pipeline):
    r = done_pipeline.repo
    types = {x["dup_type"] for x in q(r, "SELECT DISTINCT dup_type FROM duplicate_groups")}
    assert {"exact_name_phone", "exact_name_registration"} <= types
    assert r.scalar("SELECT count(*) FROM raw_records WHERE is_current") == 53                # nothing removed


def test_dry_run_writes_nothing(settings, cfg, repo, demo_dir):
    df = dry_run(settings, cfg, repo, LocalSource(demo_dir), 40)
    assert 0 < len(df) <= 40 and {"original_name", "suggested_standard_name", "confidence", "reason"} <= set(df.columns)
    assert repo.scalar("SELECT count(*) FROM raw_records") == 0 and repo.scalar("SELECT count(*) FROM master_entities") == 0


def test_output_may_not_overlap_input(settings, demo_dir):
    from app.config import ConfigError

    with pytest.raises(ConfigError):
        Pipeline(settings, RuntimeConfig(local_input_dir=str(demo_dir), output_dir=str(demo_dir / "out")), Repository(":memory:"))
