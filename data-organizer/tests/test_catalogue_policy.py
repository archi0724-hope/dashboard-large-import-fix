import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vendor_dashboard"))

from catalogue_policy import clean_and_classify_product


@pytest.mark.parametrize(
    "record",
    [
        {"product_name": "Syringe pump", "record_type": "catalogue"},
        {"product_name": "  Sterile   examination gloves  ", "record_type": "catalogue"},
        {"product_name": "Clot activator vial", "record_type": "price", "price": "₹1.40"},
        {"product_name": "Tablet press machine", "record_type": "catalogue"},
        {"product_name": "PCR diagnostic kit", "record_type": "catalogue"},
    ],
)
def test_medical_non_drug_records_are_approved_and_cleaned(record):
    cleaned, status, reason = clean_and_classify_product(record)

    assert status == "approved"
    assert cleaned["product_name"] == cleaned["product_name"].strip()
    assert reason == "Validated medical non-drug product."


@pytest.mark.parametrize(
    "product",
    [
        "Paracetamol 500 mg tablet",
        "Amoxicillin capsules",
        "Insulin injection",
        "Pharmaceutical medicine",
    ],
)
def test_medicines_are_excluded(product):
    _, status, reason = clean_and_classify_product(
        {"product_name": product, "record_type": "price", "price": 10}
    )

    assert status == "excluded"
    assert "Medicine or pharmaceutical" in reason


@pytest.mark.parametrize(
    "record,expected_reason",
    [
        ({"product_name": "Office chair", "record_type": "catalogue"}, "could not be confirmed"),
        ({"product_name": "Medical ventilator", "record_type": "price"}, "no price value"),
        ({"product_name": "Medical ventilator", "record_type": "price", "price": 0}, "outside the allowed range"),
        ({"product_name": "Surgical forceps", "record_type": "price", "price": "unknown"}, "not a valid number"),
        ({"product_name": "Medical gloves", "record_type": "catalogue", "gst": 125}, "outside the allowed range"),
    ],
)
def test_uncertain_or_invalid_records_are_held_for_review(record, expected_reason):
    _, status, reason = clean_and_classify_product(record)

    assert status == "review"
    assert expected_reason in reason
