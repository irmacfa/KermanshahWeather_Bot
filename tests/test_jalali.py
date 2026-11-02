"""Regression tests for Gregorian-to-Jalali conversion."""

import unittest

from core.jalali import format_jalali_date, to_jalali


class JalaliConversionTests(unittest.TestCase):
    def test_nowruz_1403(self):
        self.assertEqual(to_jalali(2024, 3, 20), (1403, 1, 1))

    def test_last_day_of_1403(self):
        self.assertEqual(to_jalali(2025, 3, 20), (1403, 12, 30))

    def test_nowruz_1404(self):
        self.assertEqual(format_jalali_date(1404, 1, 1), "۱۴۰۴/۱/۱")


if __name__ == "__main__":
    unittest.main()
