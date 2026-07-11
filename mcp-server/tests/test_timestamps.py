"""Timestamp wording."""
import unittest
from datetime import date, datetime

from kovault_mcp import timestamps as ts


class TestOrdinal(unittest.TestCase):
    def test_suffixes(self):
        self.assertEqual([ts.ordinal(n) for n in (1, 2, 3, 4)], ["1st", "2nd", "3rd", "4th"])

    def test_teens_are_th(self):
        self.assertEqual([ts.ordinal(n) for n in (11, 12, 13)], ["11th", "12th", "13th"])

    def test_twenties(self):
        self.assertEqual([ts.ordinal(n) for n in (21, 22, 23)], ["21st", "22nd", "23rd"])


class TestWords(unittest.TestCase):
    def test_datetime_with_time(self):
        self.assertEqual(ts.words_datetime(datetime(2020, 12, 10, 21, 20)),
                         "10th of December 2020 21:20")

    def test_midnight_drops_time(self):
        self.assertEqual(ts.words_datetime(datetime(2020, 12, 10, 0, 0)),
                         "10th of December 2020")

    def test_date_only(self):
        self.assertEqual(ts.words_datetime(date(2026, 7, 9)), "9th of July 2026")

    def test_iso_string(self):
        self.assertEqual(ts.words_datetime("2020-12-10T21:20:00"), "10th of December 2020 21:20")

    def test_none_and_bad(self):
        self.assertEqual(ts.words_datetime(None), "")
        self.assertEqual(ts.words_datetime(""), "")
        self.assertEqual(ts.words_datetime("not-a-date"), "")


if __name__ == "__main__":
    unittest.main()
