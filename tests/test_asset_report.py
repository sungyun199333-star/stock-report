import unittest
from datetime import date
from decimal import Decimal

from asset_report import make_report, parse_history


class ParseHistoryTests(unittest.TestCase):
    def test_sums_asset_rows_per_date_and_uses_latest_two_days(self):
        history = parse_history([
            ["날짜", "총액(원)"],
            ["2026-10-16", "1,000,000원"],
            ["2026-10-16", "500000"],
            ["2026-10-17", "₩1,600,000"],
        ])

        self.assertEqual(history, {
            date(2026, 10, 16): Decimal("1500000"),
            date(2026, 10, 17): Decimal("1600000"),
        })
        report = make_report(history)
        self.assertIn("총 평가액: 1,600,000 원", report)
        self.assertIn("2026-10-16 대비 +100,000 원 (+6.67%)", report)

    def test_supports_configured_headers(self):
        history = parse_history(
            [["일자", "총액"], ["2026/10/16", "100"], ["2026/10/17", "125"]],
            date_column="일자",
            value_column="총액",
        )

        self.assertEqual(list(history.values()), [Decimal("100"), Decimal("125")])

    def test_requires_two_distinct_dates(self):
        with self.assertRaisesRegex(ValueError, "최소 2개"):
            parse_history([["날짜", "평가액"], ["2026-10-17", "100"]])

    def test_reports_invalid_rows_with_row_number(self):
        with self.assertRaisesRegex(ValueError, "3행 처리 실패"):
            parse_history([
                ["날짜", "평가액"],
                ["2026-10-16", "100"],
                ["2026-10-17", "잘못된 값"],
            ])


if __name__ == "__main__":
    unittest.main()
