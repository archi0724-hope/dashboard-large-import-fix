import pytest

from app.cleaning.name_cleaner import NameCleaner
from app.cleaning.normalizer import Gazetteer, normalize_text
from app.cleaning.record_cleaner import RecordCleaner
from app.cleaning.validators import (
    gstin_checksum_char, gstin_is_valid, parse_emails, parse_phones, parse_pincode, parse_registration_id, parse_website, validate_name,
)
from app.config import RuntimeConfig


@pytest.fixture(scope="module")
def nc():
    return NameCleaner("hospital", Gazetteer())


def test_spec_examples_normalise_identically(nc):
    a = nc.clean("SMS HOSPITAL JAIPUR")
    b = nc.clean("S.M.S. Hospital Jaipur")
    c = nc.clean("SMS Hosp., Jaipur")
    assert a.normalized == b.normalized == c.normalized == "sms hospital jaipur"
    assert a.core == "sms hospital" and a.place_hint == "jaipur"


def test_ambiguous_abbreviation_is_never_forced(nc):
    f = nc.clean("St. Mary Hospital")
    assert "st" in f.tokens and "saint mary hospital" in f.variants       # only offered as a variant
    assert nc.clean("Saint Mary Hospital").core == "saint mary hospital"


def test_safe_abbreviations_expand(nc):
    assert nc.clean("Aravali Care Hosp. Pvt. Ltd.").normalized == "aravali care hospital private limited"


def test_original_is_preserved(nc):
    f = nc.clean("  S.M.S.   HOSPITAL ")
    assert f.raw == "  S.M.S.   HOSPITAL "


def test_city_synonym_and_state():
    g = Gazetteer()
    assert g.normalize_city("Gurgaon") == "gurugram" and g.normalize_state("RJ") == "rajasthan"


def test_gstin_checksum_roundtrip():
    first14 = "08AABCA1111A1Z"
    good = first14 + gstin_checksum_char(first14)
    assert gstin_is_valid(good) and not gstin_is_valid(good[:-1] + ("0" if good[-1] != "0" else "1"))
    assert parse_registration_id(good).type == "gst"
    assert parse_registration_id(good[:-1] + "X").invalid


def test_phones_handle_excel_floats_and_junk():
    assert parse_phones("9876500011.0").valid == ["+919876500011"]
    assert parse_phones("98765").invalid == ["98765"]
    assert parse_phones("9999999999").placeholder
    assert len(parse_phones("0141-2560101 / 9876500011").valid) == 2


def test_email_website_pincode():
    assert parse_emails("Info@Example.ORG, bad@@x").valid == ["info@example.org"]
    assert parse_website("www.Example.org/").domain == "example.org"
    assert parse_pincode("302 004") == ("302004", True) and parse_pincode("12345")[1] is False


def test_validate_name_flags_headers_and_totals():
    assert validate_name("Total", normalize_text("Total"))
    assert validate_name("12345", "12345")
    assert not validate_name("ABC Hospital", "abc hospital")


def test_record_cleaner_flags_but_keeps():
    rc = RecordCleaner(RuntimeConfig())
    row, flags = rc.clean({"record_id": "x", "original_name": "ABC Hospital", "original_city": "Jaipur", "original_state": "Kerala",
                           "original_phone": "12", "original_email": "nope", "original_address": "MI Road 302001"})
    kinds = {f.issue_type for f in flags}
    assert {"city_state_mismatch", "invalid_phone", "invalid_email"} <= kinds
    assert row["pincode"] == "302001" and row["is_resolvable"]
