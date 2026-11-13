"""Cleaning of messy lead data: phones, campus names, grades."""
from __future__ import annotations
import re
import phonenumbers

_GRADE_WORDS = {
    "nursery": "Nursery", "pre kg": "Pre-KG", "prekg": "Pre-KG", "pre-kg": "Pre-KG",
    "lkg": "LKG", "ukg": "UKG", "playgroup": "Playgroup", "play group": "Playgroup",
    "toddler": "Toddler",
}
_ORD = {1: "1st", 2: "2nd", 3: "3rd"}


def clean_phone(raw: str, default_region: str = "IN") -> str | None:
    """Return E.164 or None. Handles 'p:+91…', spaces, dashes, missing country code."""
    if not raw:
        return None
    s = str(raw).strip()
    s = re.sub(r"^[a-zA-Z]+:\s*", "", s)          # 'p:+91…'
    s = re.sub(r"[^\d+]", "", s)                   # keep digits and plus
    if not s:
        return None
    try:
        num = phonenumbers.parse(s, None if s.startswith("+") else default_region)
    except phonenumbers.NumberParseException:
        # maybe 91XXXXXXXXXX without plus
        try:
            num = phonenumbers.parse("+" + s, None)
        except phonenumbers.NumberParseException:
            return None
    if not phonenumbers.is_valid_number(num):
        if not s.startswith("+"):                  # 91XXXXXXXXXX typed without plus
            try:
                num2 = phonenumbers.parse("+" + s, None)
                if phonenumbers.is_valid_number(num2):
                    return phonenumbers.format_number(num2, phonenumbers.PhoneNumberFormat.E164)
            except phonenumbers.NumberParseException:
                pass
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def canonical_campus(raw: str, campus_keys: list[str] | None = None) -> str | None:
    if not raw:
        return None
    t = re.sub(r"[^a-z0-9]+", " ", raw.lower()).strip()
    compact = t.replace(" ", "")
    if "ecr" in compact or "eastcoast" in compact:
        return "ecr"
    if "tnagar" in compact or "thyagaraya" in compact or "nagar" in compact:
        return "t_nagar"
    return None


def clean_grade(raw: str) -> str:
    """Return a spoken-friendly class, or '' when unclear (e.g. a school name in the grade column)."""
    if not raw:
        return ""
    t = re.sub(r"\s+", " ", raw.strip().lower().replace("_", " "))
    for k, v in _GRADE_WORDS.items():
        if re.fullmatch(rf"(class |grade |std )?{re.escape(k)}", t):
            return v
    m = re.fullmatch(r"(?:class|grade|std|standard)?\s*(\d{1,2})(?:st|nd|rd|th)?(?:\s*(?:std|standard|grade|class))?", t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 12:
            return f"{_ORD.get(n, str(n) + 'th')} grade" if n not in (11, 12) else f"{n}th grade"
    return ""   # anything else (school names etc.) is treated as unclear


def clean_name(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip()).title()
