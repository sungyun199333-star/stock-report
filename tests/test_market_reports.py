import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_report import analyze, make_report, should_run_us_schedule


class MarketReportTests(unittest.TestCase):
    def test_moving_averages_are_listed_in_descending_price_order(self):
        closes = list(range(220, 0, -1))
        rows = [
            (float(close), 1000, float(close + 1), float(close - 1), float(close),
             datetime(2026, 10, 16, tzinfo=ZoneInfo("UTC")))
            for close in closes
        ]

        analysis = analyze(rows, "USD")
        moving_average_line = analysis.splitlines()[2]
        alignment_line = analysis.splitlines()[3]

        self.assertIn("200일 100.50 USD | 120일 60.50 USD", moving_average_line)
        self.assertIn("50일 25.50 USD | 20일 10.50 USD | 5일 3.00 USD", moving_average_line)
        self.assertIn(
            "가격순: 200일선 > 120일선 > 50일선 > 20일선 > 5일선",
            alignment_line,
        )
        self.assertIn("배열 상태: 역배열", alignment_line)

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
                patch(
                    "stock_report.get_korea_investor_flow",
                    return_value={
                        "date": "2026-10-16",
                        "개인": 100_000_000,
                        "외국인": -200_000_000,
                        "기관": 0,
                    },
                ) as get_investor_flow,
            ):
                korea_report, korea_charts = make_report(chart_dir, "korea")
                us_report, us_charts = make_report(chart_dir, "us")

        self.assertIn("SK하이닉스 (000660.KS)", korea_report)
        self.assertIn("NAVER (035420.KS)", korea_report)
        self.assertNotIn("Tesla (TSLA)", korea_report)
        self.assertEqual(
            [chart[0] for chart in korea_charts], ["000660.KS", "035420.KS"]
        )
        self.assertIn("Tesla (TSLA)", us_report)
        self.assertIn("SPAC and New Issue ETF (SPCX)", us_report)
        self.assertIn("Meta Platforms (META)", us_report)
        self.assertIn("iShares Semiconductor ETF (SOXX)", us_report)
        self.assertNotIn("SK하이닉스 (000660.KS)", us_report)
        self.assertNotIn("NAVER (035420.KS)", us_report)
        self.assertEqual(
            [chart[0] for chart in us_charts], ["TSLA", "SPCX", "META", "SOXX"]
        )
        self.assertIn("개인 순매수 1.0억 원", korea_report)
        self.assertIn("외국인 순매도 2.0억 원", korea_report)
        self.assertIn("기관 중립 0.0억 원", korea_report)
        self.assertNotIn("[투자자별 수급", us_report)
        self.assertEqual(
            [call.args[0] for call in get_investor_flow.call_args_list],
            ["000660.KS", "035420.KS"],
        )

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
