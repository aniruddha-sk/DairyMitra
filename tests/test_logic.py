"""Unit tests for fat_snf.logic (no database)."""
from datetime import date, datetime
from decimal import Decimal as D

import pytest

from fat_snf import logic as L
from fat_snf.logic import ValidationError

from conftest import REFERENCE_SETTINGS

# Every cell of the supplied reference chart image (FAT rows x SNF 8.3..8.7)
REFERENCE_IMAGE = {
    "3.2": "25.50 25.80 26.10 26.40 26.70", "3.3": "25.80 26.10 26.40 26.70 27.00",
    "3.4": "26.10 26.40 26.70 27.00 27.30", "3.5": "26.40 26.70 27.00 27.30 27.60",
    "3.6": "26.70 27.00 27.30 27.60 27.90", "3.7": "27.00 27.30 27.60 27.90 28.20",
    "3.8": "27.30 27.60 27.90 28.20 28.50", "3.9": "27.60 27.90 28.20 28.50 28.80",
    "4.0": "27.90 28.20 28.50 28.80 29.10", "4.1": "28.20 28.50 28.80 29.10 29.40",
    "4.2": "28.50 28.80 29.10 29.40 29.70", "4.3": "28.80 29.10 29.40 29.70 30.00",
    "4.4": "29.10 29.40 29.70 30.00 30.30", "4.5": "29.40 29.70 30.00 30.30 30.60",
    "4.6": "29.70 30.00 30.30 30.60 30.90", "4.7": "30.00 30.30 30.60 30.90 31.20",
    "4.8": "30.30 30.60 30.90 31.20 31.50", "4.9": "30.60 30.90 31.20 31.50 31.80",
    "5.0": "30.90 31.20 31.50 31.80 32.10",
}


class TestMatrixGeneration:
    def test_reproduces_reference_image_exactly(self):
        fats, snfs, matrix = L.generate_matrix(REFERENCE_SETTINGS)
        assert len(fats) == 19 and len(snfs) == 5
        for fat, row in REFERENCE_IMAGE.items():
            expected = [D(x) for x in row.split()]
            actual = [matrix[(D(fat).quantize(L.PAISE), s)] for s in snfs]
            assert actual == expected, fat

    def test_prompt_examples(self):
        _, _, m = L.generate_matrix(REFERENCE_SETTINGS)
        assert m[(D("3.50"), D("8.50"))] == D("27.00")
        assert m[(D("3.50"), D("8.40"))] == D("26.70")
        assert m[(D("3.50"), D("8.60"))] == D("27.30")
        assert m[(D("3.60"), D("8.50"))] == D("27.30")
        assert m[(D("3.40"), D("8.50"))] == D("26.70")

    def test_negative_step_decreases_rate(self):
        s = dict(REFERENCE_SETTINGS, snf_rate_step="-0.10")
        _, _, m = L.generate_matrix(s)
        assert m[(D("3.50"), D("8.70"))] == D("26.80")
        assert m[(D("3.50"), D("8.30"))] == D("27.20")

    def test_custom_dimensions_and_steps(self):
        s = dict(REFERENCE_SETTINGS, fat_min="6.0", fat_max="7.0", fat_step="0.5",
                 std_fat="6.5", snf_min="9.0", snf_max="9.0", snf_step="0.1", std_snf="9.0",
                 std_rate="45", fat_rate_step="1.25")
        fats, snfs, m = L.generate_matrix(s)
        assert fats == [D("6.00"), D("6.50"), D("7.00")] and snfs == [D("9.00")]
        assert m[(D("6.00"), D("9.00"))] == D("43.75")
        assert m[(D("7.00"), D("9.00"))] == D("46.25")

    def test_range_not_divisible_by_step(self):
        with pytest.raises(ValidationError) as e:
            L.generate_matrix(dict(REFERENCE_SETTINGS, fat_max="5.05"))
        assert e.value.field == "fat_max"

    def test_negative_rates_rejected(self):
        with pytest.raises(ValidationError):
            L.generate_matrix(dict(REFERENCE_SETTINGS, std_rate="0.10"))

    @pytest.mark.parametrize("field,value", [("fat_step", "0"), ("fat_step", "-0.1"),
                                             ("fat_min", "abc"), ("snf_max", "NaN"),
                                             ("fat_max", "Infinity")])
    def test_bad_settings(self, field, value):
        with pytest.raises(ValidationError):
            L.generate_matrix(dict(REFERENCE_SETTINGS, **{field: value}))

    def test_too_many_cells(self):
        with pytest.raises(ValidationError):
            L.generate_matrix(dict(REFERENCE_SETTINGS, fat_min="0.1", fat_max="9.9",
                                   snf_min="0.1", snf_max="9.9", std_fat="5.0", std_snf="5.0"))


class TestMatrixValidation:
    def _cells(self, fats, snfs, rate="27"):
        return [{"fat": f, "snf": s, "rate": rate} for f in fats for s in snfs]

    def test_valid(self):
        fats, snfs, m = L.validate_matrix(["3.5", "3.4"], ["8.5"], self._cells(["3.5", "3.4"], ["8.5"]))
        assert fats == [D("3.40"), D("3.50")] and len(m) == 2

    def test_missing_cell(self):
        cells = self._cells(["3.4", "3.5"], ["8.5"])[:1]
        with pytest.raises(ValidationError, match="रिकामे"):
            L.validate_matrix(["3.4", "3.5"], ["8.5"], cells)

    def test_duplicate_axis(self):
        with pytest.raises(ValidationError):
            L.validate_matrix(["3.5", "3.50"], ["8.5"], self._cells(["3.5"], ["8.5"]))

    def test_cell_outside_axes(self):
        with pytest.raises(ValidationError):
            L.validate_matrix(["3.5"], ["8.5"], [{"fat": "3.5", "snf": "8.5", "rate": "1"},
                                                  {"fat": "9.9", "snf": "8.5", "rate": "1"}])

    @pytest.mark.parametrize("rate", ["", "-1", "abc", None, "1e9"])
    def test_bad_rate(self, rate):
        with pytest.raises(ValidationError):
            L.validate_matrix(["3.5"], ["8.5"], [{"fat": "3.5", "snf": "8.5", "rate": rate}])


class TestParsing:
    @pytest.mark.parametrize("v", ["3.5", "3.50", 3.5, "10"])
    def test_fat_ok(self, v):
        assert L.parse_fat(v) == D(str(v)).quantize(L.PAISE)

    @pytest.mark.parametrize("v", ["", None, "abc", "0", "-1", "3.555", "NaN", "inf", "21", True])
    def test_fat_bad(self, v):
        with pytest.raises(ValidationError):
            L.parse_fat(v)

    @pytest.mark.parametrize("v", ["0", "-5", "", "abc", "1.234", "10000", "NaN"])
    def test_litres_bad(self, v):
        with pytest.raises(ValidationError):
            L.parse_litres(v)

    def test_litres_ok(self):
        assert L.parse_litres("18") == D("18.00")

    def test_date_range(self):
        assert L.parse_date_range("2026-09-21", "2026-09-30") == (date(2026, 9, 21), date(2026, 9, 30))
        with pytest.raises(ValidationError):
            L.parse_date_range("2026-09-30", "2026-09-21")
        with pytest.raises(ValidationError):
            L.parse_date_range("2024-01-01", "2026-01-01")
        with pytest.raises(ValidationError):
            L.parse_date_range("2026-13-01", "2026-13-02")


class TestMoney:
    def test_entry_amount(self):
        assert L.entry_amount(D("18.0"), D("27.00")) == D("486.00")
        assert L.entry_amount(D("12.0"), D("26.10")) == D("313.20")

    def test_half_up_rounding(self):
        assert L.entry_amount(D("12.35"), D("26.15")) == D("322.95")   # 322.9525
        assert L.entry_amount(D("0.05"), D("26.10")) == D("1.31")      # 1.305 -> HALF_UP
        assert L.money(D("2.675")) == D("2.68")                        # float would give 2.67

    def test_weighted_average(self):
        assert L.weighted_average([(D("3.5"), D("10")), (D("4.5"), D("30"))]) == D("4.25")
        assert L.weighted_average([]) is None
        assert L.weighted_average([(D("3.5"), D("0"))]) is None


class TestSlot:
    @pytest.mark.parametrize("hm,slot", [((0, 0), "morning"), ((2, 59), "morning"),
                                         ((14, 59), "morning"), ((15, 0), "evening"),
                                         ((23, 59), "evening")])
    def test_auto_slot(self, hm, slot):
        assert L.auto_slot(datetime(2026, 10, 9, *hm, tzinfo=L.IST)) == slot

    def test_ist_date_differs_from_utc(self):
        from zoneinfo import ZoneInfo
        utc = datetime(2026, 10, 8, 20, 0, tzinfo=ZoneInfo("UTC"))  # 01:30 IST next day
        assert L.today_ist(utc) == date(2026, 10, 9)
        assert L.auto_slot(utc) == "morning"


class TestWarnings:
    T = {"warn_low_litres": "0.5", "warn_high_litres": "100", "warn_deviation_pct": 60}

    def test_high_low(self):
        assert L.entry_warnings(D("150"), [], self.T)
        assert L.entry_warnings(D("0.2"), [], self.T)
        assert L.entry_warnings(D("20"), [], self.T) == []

    def test_deviation_needs_history(self):
        assert L.entry_warnings(D("50"), [D("10")] * 4, self.T) == []
        assert len(L.entry_warnings(D("50"), [D("10")] * 5, self.T)) == 1
        assert L.entry_warnings(D("12"), [D("10")] * 5, self.T) == []
