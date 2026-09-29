"""Entity-resolution behaviour on the situations named in the specification."""
import json

import pytest

from app.cleaning.name_cleaner import NameCleaner
from app.cleaning.normalizer import Gazetteer
from app.cleaning.record_cleaner import RecordCleaner
from app.config import RuntimeConfig
from app.matching.confidence import band_for
from app.matching.entity_resolution import EntityResolver
from app.matching.fuzzy_match import name_similarity


def resolve(settings, repo, records, cfg=None):
    """records: list of dicts with original_* fields -> rows resolved in ONE batch, returns {name: resolution}."""
    cfg = cfg or RuntimeConfig()
    gaz = Gazetteer()
    rc = RecordCleaner(cfg, gaz)
    rows = []
    for i, r in enumerate(records):
        raw = {"record_id": f"R{i}", "source_file": "t.csv", "source_sheet": None, "source_page": None, "source_row": i + 1, **r}
        cleaned, _ = rc.clean(raw)
        rows.append({**raw, **cleaned, "seq": i})
    res = EntityResolver(repo, cfg, settings, NameCleaner(cfg.entity_type, gaz), use_tfidf=False)
    res.load()
    res.resolve_batch(rows)
    return {r["record_id"]: r for r in repo.dicts("SELECT * FROM resolutions")}, res


def rec(name, city=None, state=None, **kw):
    d = {"original_name": name, "original_city": city, "original_state": state}
    d.update({f"original_{k}": v for k, v in kw.items()})
    return d


def same_entity(out, *ids):
    return len({out[i]["master_entity_id"] for i in ids}) == 1


def test_spec_example_variants_merge_with_evidence(settings, repo):
    out, _ = resolve(settings, repo, [
        rec("SMS HOSPITAL JAIPUR", "Jaipur", "Rajasthan", address="JLN Marg, Jaipur"),
        rec("S.M.S. Hospital Jaipur", "Jaipur", "Rajasthan", address="Jawaharlal Nehru Marg"),
        rec("Sawai Man Singh Hospital", "Jaipur", "Rajasthan", address="JLN Marg, Jaipur", phone="0141-2560101"),
    ])
    assert same_entity(out, "R0", "R1", "R2") and out["R2"]["match_status"] == "auto_matched"
    name = repo.scalar("SELECT standard_name FROM master_entities WHERE merged_into IS NULL")
    assert name == "Sawai Man Singh Hospital"                # the fullest OBSERVED spelling, not an invented one


def test_same_name_different_city_is_not_merged(settings, repo):
    out, _ = resolve(settings, repo, [rec("ABC Hospital Jaipur", "Jaipur"), rec("ABC Hospital Kota", "Kota")])
    assert not same_entity(out, "R0", "R1")
    assert {out["R1"]["match_status"]} == {"new_entity"}


def test_identical_names_in_different_states_stay_separate(settings, repo):
    out, _ = resolve(settings, repo, [rec("City Hospital", "Jaipur", "Rajasthan", phone="9876500041"),
                                      rec("City Hospital", "Ambala", "Haryana", phone="9876500131")])
    assert not same_entity(out, "R0", "R1")


def test_similar_name_alone_needs_review_not_merge(settings, repo):
    out, _ = resolve(settings, repo, [rec("Sri Ganesh Nursing Home"), rec("Shri Ganesh Nursing Home")])
    assert out["R1"]["match_status"] == "needs_review"
    assert repo.scalar("SELECT count(*) FROM review_items WHERE status = 'pending'") == 1


def test_similar_name_with_corroboration_merges(settings, repo):
    out, _ = resolve(settings, repo, [rec("Shri Ganesh Nursing Home", "Kota", address="Rangbari Road", phone="9876500051"),
                                      rec("Sri Ganesh Nursing Home", "Kota", address="Rangbari Road", phone="9876500051")])
    assert same_entity(out, "R0", "R1") and out["R1"]["match_status"] == "auto_matched"


def test_east_west_and_numbers_are_different_entities(settings, repo):
    out, _ = resolve(settings, repo, [rec("Eastern Star Hospital", "Jaipur", "Rajasthan"), rec("Western Star Hospital", "Jaipur", "Rajasthan"),
                                      rec("Sai Clinic Unit 2", "Pune"), rec("Sai Clinic Unit 3", "Pune")])
    assert not same_entity(out, "R0", "R1") and not same_entity(out, "R2", "R3")


def test_short_distinct_names_are_not_typos(settings, repo):
    out, _ = resolve(settings, repo, [rec("ABC Hospital", "Jaipur", address="12 MI Road"), rec("ABD Hospital", "Jaipur", address="12 MI Road")])
    assert not same_entity(out, "R0", "R1")


def test_registration_id_beats_name_differences(settings, repo):
    from app.cleaning.validators import gstin_checksum_char

    g = "08AABCR3333C1Z" + gstin_checksum_char("08AABCR3333C1Z")
    out, _ = resolve(settings, repo, [rec("Rajputana Surgicals Private Limited", "Jaipur", registration_id=g),
                                      rec("R S Pvt Ltd Jaipur", "Jaipur", registration_id=g)])
    assert same_entity(out, "R0", "R1")


def test_conflicting_registration_ids_block_a_merge(settings, repo):
    from app.cleaning.validators import gstin_checksum_char

    g1 = "08AABCR3333C1Z" + gstin_checksum_char("08AABCR3333C1Z")
    g2 = "08AABCX9999K1Z" + gstin_checksum_char("08AABCX9999K1Z")
    out, _ = resolve(settings, repo, [rec("Rajputana Surgicals", "Jaipur", registration_id=g1), rec("Rajputana Surgicals", "Jaipur", registration_id=g2)])
    assert not same_entity(out, "R0", "R1")


def test_ambiguous_abbreviation_goes_to_review(settings, repo):
    out, _ = resolve(settings, repo, [rec("St. Mary Hospital", "Kota", address="Talwandi"), rec("Saint Mary Hospital", "Kota")])
    assert out["R1"]["match_status"] == "needs_review"


def test_one_sided_location_goes_to_review(settings, repo):
    out, _ = resolve(settings, repo, [rec("ABC Hospital", "Jaipur"), rec("ABC Hospital")])
    assert out["R1"]["match_status"] == "needs_review"


def test_identical_names_without_any_context_are_merged(settings, repo):
    out, _ = resolve(settings, repo, [rec("Lotus Clinic"), rec("Lotus Clinic")])
    assert same_entity(out, "R0", "R1")


def test_lowering_auto_threshold_never_merges_forced_review(settings, repo):
    cfg = RuntimeConfig(auto_match_min_score=50.0)
    out, _ = resolve(settings, repo, [rec("Sri Ganesh Nursing Home"), rec("Shri Ganesh Nursing Home")], cfg)
    assert out["R1"]["match_status"] == "needs_review"          # the rule, not the number, decides


def test_automatic_matching_off_sends_everything_uncertain_to_review(settings, repo):
    cfg = RuntimeConfig(automatic_matching=False)
    out, _ = resolve(settings, repo, [rec("Shri Ganesh Nursing Home", "Kota", phone="9876500051"), rec("Sri Ganesh Nursing Home", "Kota", phone="9876500051")], cfg)
    assert out["R1"]["match_status"] == "needs_review"


def test_manual_review_off_keeps_uncertain_separate(settings, repo):
    cfg = RuntimeConfig(manual_review_required=False)
    out, _ = resolve(settings, repo, [rec("Sri Ganesh Nursing Home"), rec("Shri Ganesh Nursing Home")], cfg)
    assert out["R1"]["match_status"] == "uncertain_kept_separate" and repo.scalar("SELECT count(*) FROM review_items") == 0


def test_every_decision_is_explained(settings, repo):
    out, _ = resolve(settings, repo, [rec("Shri Ganesh Nursing Home", "Kota", phone="9876500051"), rec("Sri Ganesh Nursing Home", "Kota", phone="9876500051")])
    r = out["R1"]
    assert r["reason"] and r["match_method"] and r["match_band"] in ("Very High", "High")
    ev = {e["key"]: e["status"] for e in json.loads(r["evidence"])}
    assert ev["phone"] == "match" and ev["city"] == "match"


def test_unnamed_records_are_kept_but_unresolvable(settings, repo):
    out, _ = resolve(settings, repo, [rec("Total"), rec(None, "Jaipur")])
    assert out["R0"]["match_status"] == "unresolvable" and out["R1"]["master_entity_id"] is None


def test_name_similarity_scale():
    nc = NameCleaner("hospital", Gazetteer())
    a, b = nc.clean("Marudhar Multispeciality Hospital"), nc.clean("Marudhar Multi Speciality Hospital")
    assert name_similarity(a, b, nc.distinguishing).score >= 85
    assert name_similarity(nc.clean("ABC Hospital"), nc.clean("ABC Hospital"), nc.distinguishing).score == 100
    assert name_similarity(nc.clean("Aravali Care Hospital"), nc.clean("Marudhar Care Hospital"), nc.distinguishing).score < 70


def test_bands():
    t = RuntimeConfig().thresholds
    assert [band_for(s, t) for s in (99, 90, 75, 55, 10)] == ["Very High", "High", "Possible Match", "Manual Review", "Probably Different"]
