"""Clean, validate, and scope vendor product rows for the catalogue."""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping

TEXT_FIELDS = (
    "product_name",
    "sku",
    "description",
    "unit",
    "currency",
    "brand",
    "product_category",
    "specification",
    "pack_size",
    "moq",
    "availability",
    "source_file",
    "source_sheet",
    "source_page",
    "image_reference",
)

DRUG_PATTERN = re.compile(
    r"\b(?:"
    r"pharmaceuticals?|medicines?|medications?|drugs?|antibiotics?|antivirals?|"
    r"antifungals?|analgesics?|painkillers?|vaccines?|insulin|paracetamol|"
    r"acetaminophen|amoxicillin|azithromycin|ibuprofen|metformin|"
    r"atorvastatin|omeprazole|cetirizine|doxycycline|ciprofloxacin|"
    r"dosage\s+form|tablets?|capsules?|syrups?|elixirs?|suppositories|"
    r"injectables?|ophthalmic\s+drops|eye\s+drops|ear\s+drops|oral\s+drops"
    r")\b",
    re.IGNORECASE,
)

DEVICE_CONTEXT_PATTERN = re.compile(
    r"\b(?:tablet\s+(?:press|counter|counting)|capsule\s+(?:filling|polishing)\s+machine|"
    r"pill\s+cutter|syringe\s+pump|infusion\s+pump|injection\s+mould|"
    r"injection\s+molding|vaccine\s+refrigerator)\b",
    re.IGNORECASE,
)

MEDICAL_PATTERN = re.compile(
    r"\b(?:"
    r"medical|healthcare|health\s+care|hospital|clinical|surgical|surgery|"
    r"diagnostic|diagnostics|laboratory|laboratories|pathology|radiology|"
    r"dental|dentistry|ophthalmic|optical|patient\s+care|medical\s+device|"
    r"ventilator|defibrillator|electrocardiograph|ecg|ekg|patient\s+monitor|"
    r"pulse\s+oximeter|oximeter|ultrasound|ultrasonic\s+scanner|x[\s-]?ray|"
    r"mri|ct\s+scanner|endoscope|endoscopy|laparoscope|anesthesia|anaesthesia|"
    r"centrifuge|microscope|spectrophotometer|autoclave|sterili[sz]er|"
    r"incubator|dialysis|hemodialysis|haemodialysis|electrosurgical|"
    r"infusion\s+pump|syringe\s+pump|surgical\s+instrument|forceps|scalpel|"
    r"catheter|cannula|stethoscope|thermometer|sphygmomanometer|"
    r"blood\s+pressure\s+monitor|glucometer|glucose\s+meter|"
    r"blood\s+glucose\s+test|test\s+strip|specimen|vacutainer|"
    r"blood\s+collection|clot\s+activator\s+vials?|urine\s+containers?|test\s+tubes?|culture\s+media|"
    r"vacuum\s+(?:esr|pt)\b|"
    r"petri\s+dish|pipette|micropipette|reagent|assay|pcr\s+(?:kit|test)|"
    r"rapid\s+test|diagnostic\s+kit|laboratory\s+kit|lab\s+(?:kit|equipment|consumable)|"
    r"examination\s+gloves?|surgical\s+gloves?|medical\s+gloves?|latex\s+gloves?|"
    r"nitrile\s+gloves?|surgical\s+masks?|medical\s+masks?|face\s+masks?|"
    r"personal\s+protective\s+equipment|ppe|surgical\s+gown|isolation\s+gown|"
    r"gauze|dressing|bandage|wound\s+care|suture|sutures|surgical\s+thread|"
    r"hypodermic\s+needle|disposable\s+needle|medical\s+syringe|"
    r"iv\s+set|infusion\s+set|blood\s+transfusion\s+set|"
    r"wheelchair|hospital\s+bed|patient\s+bed|examination\s+table|"
    r"mobility\s+aid|orthopedic|orthopaedic|prosthe[st]is|implant|sterile\s+gloves|"
    r"medical\s+disposable|clinical\s+consumable|surgical\s+consumable|"
    r"sterile\s+(?:drapes?|dressings?|gloves?|syringes?|needles?|tubing)|"
    r"urine\s+analy[sz]er|hematology\s+analy[sz]er|haematology\s+analy[sz]er|"
    r"biochemistry\s+analy[sz]er|clinical\s+chemistry\s+analy[sz]er|"
    r"blood\s+bank|blood\s+bag|medical\s+refrigerator|vaccine\s+refrigerator"
    r")\b",
    re.IGNORECASE,
)

_NUMERIC_PRICE = re.compile(
    r"^\s*(?:(₹|INR)\s*)?([+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:INR)?\s*$",
    re.IGNORECASE,
)
def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(character if character.isprintable() else " " for character in text)
    return re.sub(r"\s+", " ", text).strip()


def _validated_number(value: object, label: str, *, minimum: float, maximum: float) -> tuple[float | None, str]:
    if value is None or value == "":
        return None, ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"{label} is not a valid number."
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None, f"{label} is outside the allowed range."
    return number, ""


def clean_and_classify_product(
    record: Mapping[str, object], *, allow_price_lists: bool = False
) -> tuple[dict, str, str]:
    """Return normalized row, status (approved/review/excluded), and explanation."""
    cleaned = dict(record)
    for field in TEXT_FIELDS:
        cleaned[field] = _clean_text(cleaned.get(field, ""))

    product = cleaned["product_name"]
    if len(product) < 2 or not any(character.isalpha() for character in product):
        return cleaned, "review", "Product name is empty or not a usable product description."

    classification_text = " ".join(
        str(cleaned.get(field, ""))
        for field in (
            "product_name",
            "description",
            "product_category",
            "specification",
            "pack_size",
            "unit",
        )
    )
    device_context = bool(DEVICE_CONTEXT_PATTERN.search(classification_text))
    if DRUG_PATTERN.search(classification_text) and not device_context:
        return cleaned, "excluded", "Medicine or pharmaceutical product; excluded from the non-drug catalogue."

    price = cleaned.get("price")
    if price is not None and price != "":
        match = _NUMERIC_PRICE.fullmatch(str(price)) if isinstance(price, str) else None
        if match:
            cleaned["price"] = float(match.group(2).replace(",", ""))
            if match.group(1) == "₹":
                cleaned["currency"] = cleaned["currency"] or "INR"
        elif allow_price_lists and isinstance(price, str):
            price_values = [value.strip() for value in price.split(",")]
            matches = [_NUMERIC_PRICE.fullmatch(value) for value in price_values]
            if len(price_values) > 1 and all(matches):
                if any(match.group(1) == "₹" for match in matches if match):
                    cleaned["currency"] = cleaned["currency"] or "INR"
            else:
                number, error = _validated_number(price, "Price", minimum=0.01, maximum=float("inf"))
                if error:
                    return cleaned, "review", error
                cleaned["price"] = number
        else:
            number, error = _validated_number(price, "Price", minimum=0.01, maximum=float("inf"))
            if error:
                return cleaned, "review", error
            cleaned["price"] = number
    elif str(cleaned.get("record_type", "")).casefold() == "price":
        return cleaned, "review", "Price record has no price value."

    for field, label, maximum in (
        ("mrp", "MRP", float("inf")),
        ("discount", "Discount", 100.0),
        ("gst", "GST", 100.0),
    ):
        value = cleaned.get(field)
        if value is None or value == "":
            continue
        number, error = _validated_number(value, label, minimum=0, maximum=maximum)
        if error:
            return cleaned, "review", error
        cleaned[field] = number

    if not device_context and not MEDICAL_PATTERN.search(classification_text):
        return cleaned, "review", "Medical non-drug product could not be confirmed from its name or specifications."
    return cleaned, "approved", "Validated medical non-drug product."
