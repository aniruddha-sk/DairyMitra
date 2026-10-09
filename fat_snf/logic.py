"""
Pure FAT/SNF business logic: parsing, matrix generation, money maths,
date/slot rules and warnings. No database or Flask imports, so every rule
here is unit-testable.

Rounding policy
---------------
* FAT and SNF are stored with 2 decimals and matched EXACTLY against the
  chart (no nearest-value guessing).
* Litres are stored with 2 decimals.
* Rate per litre is stored with 2 decimals (as configured in the chart).
* Entry amount = litres x rate, rounded once, HALF_UP, to 0.01 (paise).
* Every total is the sum of already-rounded entry amounts, so a receipt's
  rows always add up exactly to its totals.
"""
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PAISE = Decimal("0.01")
ZERO = Decimal("0.00")

MILK_TYPES = ("cow", "buffalo")
CHART_MILK_TYPES = ("cow", "buffalo", "both")
SLOTS = ("morning", "evening")

# Hard limits (critical constraints). Soft limits live in owner settings.
FAT_LIMITS = (Decimal("0.10"), Decimal("20.00"))
SNF_LIMITS = (Decimal("0.10"), Decimal("20.00"))
RATE_LIMITS = (Decimal("0.00"), Decimal("9999.99"))
LITRE_LIMITS = (Decimal("0.01"), Decimal("9999.99"))
MAX_AXIS_VALUES = 100
MAX_CELLS = 2500
MAX_RANGE_DAYS = 366

SLOT_LABELS_MR = {"morning": "सकाळ", "evening": "संध्याकाळ"}
TYPE_LABELS_MR = {"cow": "गाय", "buffalo": "म्हैस", "both": "गाय व म्हैस"}


class ValidationError(ValueError):
    """A user-facing validation problem. `field` names the offending input."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.message = message
        self.field = field


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def to_decimal(value, field, message):
    if value is None or isinstance(value, bool):
        raise ValidationError(message, field)
    text = str(value).strip()
    if not text:
        raise ValidationError(message, field)
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValidationError(message, field)
    if not result.is_finite():
        raise ValidationError(message, field)
    return result


def _bounded(value, field, message, limits, places=PAISE):
    result = to_decimal(value, field, message)
    if result != result.quantize(places):
        # 3.555 is not silently turned into 3.56: FAT/SNF are matched exactly.
        raise ValidationError(message, field)
    lo, hi = limits
    if result < lo or result > hi:
        raise ValidationError(message, field)
    return result.quantize(places)


def parse_fat(value):
    return _bounded(value, "fat", "कृपया योग्य FAT मूल्य भरा.", FAT_LIMITS)


def parse_snf(value):
    return _bounded(value, "snf", "कृपया योग्य SNF मूल्य भरा.", SNF_LIMITS)


def parse_litres(value):
    result = to_decimal(value, "litres", "दूधाचे लिटर शून्यापेक्षा जास्त असावे.")
    if result <= 0:
        raise ValidationError("दूधाचे लिटर शून्यापेक्षा जास्त असावे.", "litres")
    return _bounded(value, "litres", "कृपया योग्य लिटर भरा (जास्तीत जास्त 2 दशांश).",
                    LITRE_LIMITS)


def parse_rate(value, field="rate"):
    return _bounded(value, field, "कृपया योग्य दर भरा.", RATE_LIMITS)


def parse_milk_type(value, allowed=MILK_TYPES):
    v = str(value or "").strip().lower()
    if v not in allowed:
        raise ValidationError("कृपया दुधाचा प्रकार निवडा.", "milk_type")
    return v


def parse_slot(value):
    v = str(value or "").strip().lower()
    if v not in SLOTS:
        raise ValidationError("कृपया वेळ (सकाळ/संध्याकाळ) निवडा.", "slot")
    return v


def parse_date(value, field="date"):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("कृपया योग्य तारीख निवडा.", field)


def parse_date_range(from_value, to_value, max_days=MAX_RANGE_DAYS):
    start = parse_date(from_value, "from_date")
    end = parse_date(to_value, "to_date")
    if start > end:
        raise ValidationError("सुरुवातीची तारीख शेवटच्या तारखेपेक्षा नंतरची असू शकत नाही.",
                              "from_date")
    if (end - start).days + 1 > max_days:
        raise ValidationError(f"एका वेळी जास्तीत जास्त {max_days} दिवसांचा कालावधी निवडा.",
                              "to_date")
    return start, end


# ----------------------------------------------------------------------
# Money
# ----------------------------------------------------------------------
def money(value):
    return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)


def entry_amount(litres, rate):
    return money(Decimal(litres) * Decimal(rate))


def weighted_average(pairs):
    """pairs: iterable of (value, litres). Quantity-weighted average, or None."""
    total_l = ZERO
    total = Decimal(0)
    for value, litres in pairs:
        if value is None or litres is None:
            continue
        total_l += Decimal(litres)
        total += Decimal(value) * Decimal(litres)
    if total_l <= 0:
        return None
    return (total / total_l).quantize(PAISE, rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# Date and slot (Asia/Kolkata)
# ----------------------------------------------------------------------
def now_ist():
    return datetime.now(IST)


def today_ist(now=None):
    return (now or now_ist()).astimezone(IST).date()


def auto_slot(now=None):
    """15:00-23:59 IST -> evening, otherwise morning."""
    hour = (now or now_ist()).astimezone(IST).hour
    return "evening" if 15 <= hour <= 23 else "morning"


def date_range(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ----------------------------------------------------------------------
# Matrix
# ----------------------------------------------------------------------
def axis_values(minimum, maximum, step, label):
    """Inclusive arithmetic sequence of Decimals. The range must divide
    exactly by the step so the chart never has a surprise last value."""
    lo = to_decimal(minimum, f"{label}_min", f"{label.upper()} किमान मूल्य चुकीचे आहे.")
    hi = to_decimal(maximum, f"{label}_max", f"{label.upper()} कमाल मूल्य चुकीचे आहे.")
    st = to_decimal(step, f"{label}_step", f"{label.upper()} वाढ (step) चुकीची आहे.")
    if st <= 0 or st != st.quantize(PAISE):
        raise ValidationError(f"{label.upper()} वाढ (step) 0.01 पेक्षा कमी नसावी.",
                              f"{label}_step")
    if lo > hi:
        raise ValidationError(f"{label.upper()} किमान मूल्य कमाल मूल्यापेक्षा जास्त आहे.",
                              f"{label}_min")
    span = hi - lo
    if span % st != 0:
        raise ValidationError(
            f"{label.upper()} श्रेणी {lo}–{hi} ही वाढ {st} ने समान भागली जात नाही.",
            f"{label}_max")
    count = int(span / st) + 1
    if count > MAX_AXIS_VALUES:
        raise ValidationError(
            f"{label.upper()} ची जास्तीत जास्त {MAX_AXIS_VALUES} मूल्ये असू शकतात.",
            f"{label}_max")
    return [(lo + st * i).quantize(PAISE) for i in range(count)]


def generate_matrix(settings):
    """
    settings keys: fat_min, fat_max, fat_step, snf_min, snf_max, snf_step,
                   std_fat, std_snf, std_rate, fat_rate_step, snf_rate_step

    rate(fat, snf) = std_rate
                     + (fat - std_fat) / fat_step * fat_rate_step
                     + (snf - std_snf) / snf_step * snf_rate_step

    A positive *_rate_step raises the rate as FAT/SNF increase; a negative
    one lowers it. Returns (fat_values, snf_values, {(fat, snf): rate}).
    """
    fats = axis_values(settings.get("fat_min"), settings.get("fat_max"),
                       settings.get("fat_step"), "fat")
    snfs = axis_values(settings.get("snf_min"), settings.get("snf_max"),
                       settings.get("snf_step"), "snf")
    if len(fats) * len(snfs) > MAX_CELLS:
        raise ValidationError(f"चार्टमध्ये जास्तीत जास्त {MAX_CELLS} दर असू शकतात.")

    std_fat = parse_fat(settings.get("std_fat"))
    std_snf = parse_snf(settings.get("std_snf"))
    std_rate = parse_rate(settings.get("std_rate"), "std_rate")
    fat_inc = to_decimal(settings.get("fat_rate_step"), "fat_rate_step",
                         "FAT प्रति वाढ दरातील बदल चुकीचा आहे.")
    snf_inc = to_decimal(settings.get("snf_rate_step"), "snf_rate_step",
                         "SNF प्रति वाढ दरातील बदल चुकीचा आहे.")
    fat_step = Decimal(str(settings.get("fat_step")))
    snf_step = Decimal(str(settings.get("snf_step")))

    matrix = {}
    for f in fats:
        for s in snfs:
            rate = (std_rate
                    + (f - std_fat) / fat_step * fat_inc
                    + (s - std_snf) / snf_step * snf_inc)
            rate = money(rate)
            if rate < 0:
                raise ValidationError(
                    f"FAT {f} / SNF {s} साठी दर शून्यापेक्षा कमी येतो. वाढ/श्रेणी तपासा.")
            matrix[(f, s)] = rate
    return fats, snfs, matrix


def validate_matrix(fat_values, snf_values, cells):
    """
    Validate a submitted (possibly hand-edited) matrix.
    cells: iterable of {"fat", "snf", "rate"}.
    Returns (fats, snfs, {(fat, snf): rate}) with every combination present.
    """
    if not isinstance(fat_values, list) or not fat_values:
        raise ValidationError("FAT मूल्ये आवश्यक आहेत.")
    if not isinstance(snf_values, list) or not snf_values:
        raise ValidationError("SNF मूल्ये आवश्यक आहेत.")
    if len(fat_values) > MAX_AXIS_VALUES or len(snf_values) > MAX_AXIS_VALUES:
        raise ValidationError(f"FAT/SNF ची जास्तीत जास्त {MAX_AXIS_VALUES} मूल्ये असू शकतात.")
    fats = sorted(parse_fat(v) for v in fat_values)
    snfs = sorted(parse_snf(v) for v in snf_values)
    if len(set(fats)) != len(fats):
        raise ValidationError("FAT मूल्ये पुनरावृत्त आहेत.")
    if len(set(snfs)) != len(snfs):
        raise ValidationError("SNF मूल्ये पुनरावृत्त आहेत.")
    if len(fats) * len(snfs) > MAX_CELLS:
        raise ValidationError(f"चार्टमध्ये जास्तीत जास्त {MAX_CELLS} दर असू शकतात.")

    expected = {(f, s) for f in fats for s in snfs}
    if not isinstance(cells, list):
        raise ValidationError("दर तक्ता आवश्यक आहे.")
    matrix = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValidationError("दर तक्त्यातील नोंद चुकीची आहे.")
        f = parse_fat(cell.get("fat"))
        s = parse_snf(cell.get("snf"))
        if (f, s) in matrix:
            raise ValidationError(f"FAT {f} / SNF {s} दोनदा आले आहे.")
        if (f, s) not in expected:
            raise ValidationError(f"FAT {f} / SNF {s} चार्टच्या श्रेणीबाहेर आहे.")
        try:
            matrix[(f, s)] = parse_rate(cell.get("rate"))
        except ValidationError:
            raise ValidationError(f"FAT {f} / SNF {s} साठी योग्य दर भरा.", "rate")
    missing = expected - set(matrix)
    if missing:
        f, s = sorted(missing)[0]
        raise ValidationError(
            f"{len(missing)} दर रिकामे आहेत (उदा. FAT {f} / SNF {s}). सर्व दर भरा.")
    return fats, snfs, matrix


# ----------------------------------------------------------------------
# Soft warnings (never block; the user confirms and saves anyway)
# ----------------------------------------------------------------------
def entry_warnings(litres, recent_litres, thresholds):
    """
    litres: Decimal for the new entry
    recent_litres: list of Decimal from this vendor's recent entries
    thresholds: {"warn_low_litres", "warn_high_litres", "warn_deviation_pct"}
    """
    warnings = []
    low = Decimal(str(thresholds.get("warn_low_litres") or "0"))
    high = Decimal(str(thresholds.get("warn_high_litres") or "0"))
    pct = int(thresholds.get("warn_deviation_pct") or 0)

    if high > 0 and litres > high:
        warnings.append(f"लिटर ({litres}) नेहमीच्या कमाल मर्यादेपेक्षा ({high}) जास्त आहे.")
    if low > 0 and litres < low:
        warnings.append(f"लिटर ({litres}) खूप कमी आहे (मर्यादा {low}).")

    history = [Decimal(x) for x in recent_litres if x is not None]
    if pct > 0 and len(history) >= 5:
        avg = sum(history) / len(history)
        if avg > 0:
            deviation = abs(litres - avg) / avg * 100
            if deviation > pct:
                warnings.append(
                    f"या ग्राहकाच्या सरासरी दुधापेक्षा ({money(avg)} लि.) "
                    f"{int(deviation)}% फरक आहे.")
    return warnings
