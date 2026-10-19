import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_report import make_report, should_run_us_schedule


class MarketReportTests(unittest.TestCase):
    def test_korea_and_us_reports_only_include_their_market_tickers(self):
        sample_rows = [
            (100.0, 1000, 101.0, 99.0, 99.5, datetime(2026, 10, 16, tzinfo=ZoneInfo("UTC")))
        ] * 80

        with tempfile.TemporaryDirectory() as chart_dir:
            with (
                patch("stock_report.get_macro_summary", return_value="macro"),
                patch("stock_report.get_prices", return_value=(sample_rows, "USD")),
                patch("stock_report.create_chart", side_effect=lambda ticker, *_: f"{ticker}.png"),
                patch("stock_report.get_news", return_value=[]),
            ):
                korea_report, korea_charts = make_report(chart_dir, "korea")
                us_report, us_charts = make_report(chart_dir, "us")

        self.assertIn("SK하이닉스 (000660.KS)", korea_report)
        self.assertNotIn("Tesla (TSLA)", korea_report)
        self.assertEqual([chart[0] for chart in korea_charts], ["000660.KS"])
        self.assertIn("Tesla (TSLA)", us_report)
        self.assertIn("SPAC and New Issue ETF (SPCX)", us_report)
        self.assertIn("Meta Platforms (META)", us_report)
        self.assertNotIn("SK하이닉스 (000660.KS)", us_report)
        self.assertEqual([chart[0] for chart in us_charts], ["TSLA", "SPCX", "META"])

    def test_us_schedule_selects_daylight_or_standard_time_cron(self):
        eastern = ZoneInfo("America/New_York")
        daylight = datetime(2026, 7, 1, 16, 30, tzinfo=eastern)
        standard = datetime(2026, 1, 15, 16, 30, tzinfo=eastern)

        self.assertTrue(should_run_us_schedule("30 20 * * 1-5", daylight))
        self.assertFalse(should_run_us_schedule("30 21 * * 1-5", daylight))
        self.assertTrue(should_run_us_schedule("30 21 * * 1-5", standard))
        self.assertFalse(should_run_us_schedule("30 20 * * 1-5", standard))
        self.assertTrue(should_run_us_schedule("", standard))


if __name__ == "__main__":
    unittest.main()
