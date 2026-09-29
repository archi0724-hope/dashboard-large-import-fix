"""Field validators.

Each ``parse_*`` function returns the normalised value(s) *and* what was invalid, so the caller can
raise a data-quality flag while still preserving the original text. Invalid values are never used as
matching evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import phonenumbers

from .normalizer import basic_clean, normalize_text, strip_null_like


@dataclass
class Flag:
    issue_type: str
    severity: str          # 'error' | 'warning' | 'info'
    field: str
    value: str
    message: str

    def to_dict(self) -> dict:
        return {"issue_type": self.issue_type, "severity": self.severity, "field": self.field,
                "value": self.value, "message": self.message}


# ---------------------------------------------------------------------------
# Phones
# ---------------------------------------------------------------------------
@dataclass
class PhoneInfo:
    valid: list[str] = field(default_factory=list)          # E.164, de-duplicated, in order
    invalid: list[str] = field(default_factory=list)
    placeholder: list[str] = field(default_factory=list)


_PHONE_SPLIT = re.compile(r"[,;/|\n]+|\s+(?:and|or)\s+", re.I)
_LEADING_LABEL = re.compile(r"^[A-Za-z][A-Za-z .:\-()/]*?(?=[+(]?\d)")


def _fix_numeric_artifacts(s: str) -> str:
    """Excel often stores phones as floats: '9414012345.0' or '9.414012345E9'."""
    s = s.strip()
    if re.fullmatch(r"\d+\.0+", s):
        return s.split(".")[0]
    if re.fullmatch(r"\d(?:\.\d+)?[eE]\+?\d+", s):
        try:
            return str(int(Decimal(s)))
        except (InvalidOperation, ValueError):
            return s
    return s


def _is_placeholder_number(digits: str) -> bool:
    d = digits[-10:] if len(digits) >= 10 else digits
    if len(set(d)) == 1:
        return True
    return d in {"1234567890", "0123456789", "9876543210", "0987654321"}


def parse_phones(raw: str, region: str = "IN") -> PhoneInfo:
    info = PhoneInfo()
    raw = strip_null_like(raw)
    if not raw:
        return info
    seen: set[str] = set()
    for part in [p.strip() for p in _PHONE_SPLIT.split(raw) if p and p.strip()]:
        part = _fix_numeric_artifacts(part)
        part = _LEADING_LABEL.sub("", part).strip()
        digits = re.sub(r"\D", "", part)
        if not digits:
            info.invalid.append(part)
            continue
        candidates = [part]
        # "9414012345 9414012346": several numbers separated only by whitespace
        groups = [g for g in re.split(r"\s{2,}|\s(?=[6-9]\d{9}\b)", part) if g.strip()]
        if len(digits) > 13 and len(groups) > 1:
            candidates = groups
        for cand in candidates:
            cd = re.sub(r"\D", "", cand)
            if not cd:
                continue
            if _is_placeholder_number(cd):
                info.placeholder.append(cand.strip())
                continue
            try:
                num = phonenumbers.parse(cand, region)
            except phonenumbers.NumberParseException:
                info.invalid.append(cand.strip())
                continue
            if phonenumbers.is_valid_number(num):
                e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
                if e164 not in seen:
                    seen.add(e164)
                    info.valid.append(e164)
            else:
                info.invalid.append(cand.strip())
    return info


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------
_EMAIL_TOKEN = re.compile(r"[^\s,;<>()\[\]\"'|]+@[^\s,;<>()\[\]\"'|]+")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._%+\-]*[A-Za-z0-9_])?@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$"
)
_PLACEHOLDER_LOCALS = {"na", "none", "null", "noemail", "nomail", "no", "test", "abc", "xyz", "xxx", "email", "mail", "nil"}
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "rediffmail.com", "aol.com", "icloud.com", "protonmail.com", "msn.com", "nic.in", "gov.in",
}


@dataclass
class EmailInfo:
    valid: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


def parse_emails(raw: str) -> EmailInfo:
    info = EmailInfo()
    raw = strip_null_like(raw)
    if not raw:
        return info
    tokens = [t.strip(".:-") for t in _EMAIL_TOKEN.findall(raw)]
    if not tokens:
        info.invalid.append(raw)
        return info
    seen: set[str] = set()
    for t in tokens:
        low = t.lower()
        local = low.split("@", 1)[0]
        if _EMAIL_RE.match(t) and ".." not in t and local not in _PLACEHOLDER_LOCALS:
            if low not in seen:
                seen.add(low)
                info.valid.append(low)
        else:
            info.invalid.append(t)
    return info


def email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


# ---------------------------------------------------------------------------
# Websites
# ---------------------------------------------------------------------------
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
GENERIC_WEB_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com", "justdial.com",
    "practo.com", "google.com", "goo.gl", "bit.ly", "wa.me", "indiamart.com", "sulekha.com", "wikipedia.org",
}


@dataclass
class WebsiteInfo:
    url: str = ""          # normalised 'https://host/path'
    domain: str = ""       # registrable host without 'www.'
    valid: bool = False
    raw: str = ""


def parse_website(raw: str) -> WebsiteInfo:
    raw = strip_null_like(raw)
    info = WebsiteInfo(raw=raw)
    if not raw:
        return info
    if "@" in raw and not raw.lower().startswith(("http://", "https://")):
        return info  # an email in the website column
    candidate = raw.strip().split()[0].rstrip(".,;")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", candidate):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return info
    if parsed.scheme not in ("http", "https") or not _HOST_RE.match(host):
        return info
    domain = host[4:] if host.startswith("www.") else host
    path = parsed.path.rstrip("/")
    info.url = f"https://{domain}{path}"
    info.domain = domain
    info.valid = True
    return info


# ---------------------------------------------------------------------------
# Pincode
# ---------------------------------------------------------------------------
def parse_pincode(raw: str) -> tuple[str, bool]:
    raw = strip_null_like(raw)
    if not raw:
        return "", True
    raw = _fix_numeric_artifacts(raw)
    m = re.search(r"(?<!\d)([1-9]\d{2})\s?(\d{3})(?!\d)", raw)
    if m:
        return m.group(1) + m.group(2), True
    return raw, False


# ---------------------------------------------------------------------------
# Registration identifiers (GST / Udyam / CIN / PAN / other)
# ---------------------------------------------------------------------------
_GST_RE = re.compile(r"^(\d{2})([A-Z]{5}\d{4}[A-Z])([A-Z\d])Z([A-Z\d])$")
_UDYAM_RE = re.compile(r"^UDYAM-?([A-Z]{2})-?(\d{2})-?(\d{7})$")
_CIN_RE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")
_PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_VALID_GST_STATE_CODES = {f"{i:02d}" for i in range(1, 39)} | {"97", "99"}
_ID_LABEL = re.compile(
    r"^(?:udyam\s+registration\s+(?:no|number)|gstin|gst|cin|pan|reg(?:istration)?|licen[cs]e|no|number|id)\b[\s.:#\-]*",
    re.I,
)
_ID_PRIORITY = {"gst": 0, "cin": 1, "udyam": 2, "pan": 3, "other": 4}
GLOBALLY_UNIQUE_ID_TYPES = {"gst", "cin", "udyam", "pan"}


def gstin_checksum_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        val = _GST_CHARS.index(ch)
        prod = val * (1 if i % 2 == 0 else 2)
        total += prod // 36 + prod % 36
    return _GST_CHARS[(36 - total % 36) % 36]


def gstin_is_valid(gstin: str) -> bool:
    g = gstin.upper()
    m = _GST_RE.match(g)
    return bool(m) and m.group(1) in _VALID_GST_STATE_CODES and gstin_checksum_char(g[:14]) == g[14]


@dataclass
class RegistrationInfo:
    value: str = ""        # primary normalised identifier
    type: str = ""         # gst | udyam | cin | pan | other
    valid: bool = False
    all_values: list[tuple[str, str]] = field(default_factory=list)   # (type, value)
    invalid: list[str] = field(default_factory=list)
    message: str = ""


def parse_registration_id(raw: str) -> RegistrationInfo:
    info = RegistrationInfo()
    raw = strip_null_like(raw)
    if not raw:
        return info
    tokens = [t for t in re.split(r"[,;|\n]+|\s{2,}", raw) if t.strip()]
    found: list[tuple[str, str]] = []
    for tok in tokens:
        tok = tok.strip()
        while True:
            stripped = _ID_LABEL.sub("", tok)
            if stripped == tok:
                break
            tok = stripped
        norm = re.sub(r"[\s\-_.]", "", tok).upper()
        if not norm:
            continue
        if _GST_RE.match(norm):
            if gstin_is_valid(norm):
                found.append(("gst", norm))
            else:
                info.invalid.append(tok)
                info.message = "GSTIN failed state-code/checksum validation"
        elif (m := _UDYAM_RE.match(norm)):
            found.append(("udyam", f"UDYAM-{m.group(1)}-{m.group(2)}-{m.group(3)}"))
        elif _CIN_RE.match(norm):
            found.append(("cin", norm))
        elif _PAN_RE.match(norm):
            found.append(("pan", norm))
        elif re.fullmatch(r"[A-Z0-9/]{5,}", norm) and re.search(r"\d", norm):
            found.append(("other", norm))
        else:
            info.invalid.append(tok)
    if found:
        found.sort(key=lambda t: _ID_PRIORITY[t[0]])
        info.all_values = found
        info.type, info.value = found[0]
        info.valid = True
    return info


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
_HEADER_LIKE = {
    "name", "sr no", "sr", "sno", "s no", "sl no", "slno", "serial no", "serial number", "total", "grand total",
    "sub total", "subtotal", "hospital name", "company name", "name of hospital", "name of company", "address",
    "city", "state", "district", "phone", "email", "contact", "remarks", "note", "notes", "page", "no", "s n",
    "particulars", "description", "details", "entity name", "organization", "organisation",
}
_PAGE_FOOTER = re.compile(r"^(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?$")


def validate_name(raw: str, normalized: str) -> list[Flag]:
    flags: list[Flag] = []
    text = basic_clean(raw)
    if not strip_null_like(raw) or not normalized:
        flags.append(Flag("missing_name", "error", "name", text, "Name is missing or empty"))
        return flags
    if normalized in _HEADER_LIKE or _PAGE_FOOTER.match(normalized) or normalized.replace(" ", "").isdigit():
        flags.append(Flag("invalid_name", "error", "name", text,
                          "Value looks like a header, total, page number or serial number, not an entity name"))
    elif len(normalized) < 2:
        flags.append(Flag("invalid_name", "error", "name", text, "Name is shorter than 2 characters"))
    elif len(text) > 160:
        flags.append(Flag("suspicious_name", "warning", "name", text[:120] + "...",
                          "Name is unusually long - possibly a sentence or OCR noise"))
    elif not re.search(r"[^\W\d_]", text):
        flags.append(Flag("invalid_name", "error", "name", text, "Name contains no letters"))
    return flags
