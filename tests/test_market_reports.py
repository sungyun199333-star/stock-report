import tempfile
import unittest
import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_report import (
    aggregate_four_hour_closes,
    aggregate_weekly_closes,
    analyze,
    calculate_rsi_series,
    detect_rsi_divergence,
    detect_rsi_divergence_details,
    format_rsi_divergences,
    format_option_interest_levels,
    get_option_interest_levels,
    get_rsi_divergences,
    make_report,
    RsiDivergenceReport,
    should_run_us_schedule,
)


class MarketReportTests(unittest.TestCase):
    def test_option_interest_levels_aggregate_nearby_expirations_by_strike(self):
        now = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))
        expirations = [
            int((now + timedelta(days=days)).timestamp())
            for days in (10, 20, 50)
        ]

        def payload(chain):
            return json.dumps(
                {
                    "optionChain": {
                        "result": [
                            {
                                "expirationDates": expirations,
                                "options": [chain],
                            }
                        ]
                    }
                }
            ).encode()

        first_chain = {
            "calls": [
                {"strike": 100, "openInterest": 100, "volume": 10},
                {"strike": 105, "openInterest": 200, "volume": 20},
                {"strike": 120, "openInterest": 999, "volume": 99},
            ],
            "puts": [{"strike": 95, "openInterest": 150, "volume": 15}],
        }
        second_chain = {
            "calls": [{"strike": 100, "openInterest": 250, "volume": 25}],
            "puts": [{"strike": 95, "openInterest": 350, "volume": 35}],
        }
        with patch(
            "stock_report.fetch",
            side_effect=[payload(first_chain), payload(second_chain)],
        ) as fetch:
            levels = get_option_interest_levels("TSLA", 100, now)

        self.assertEqual(len(fetch.call_args_list), 2)
        self.assertEqual(levels["expirations"], [
            (now + timedelta(days=10)).date(),
            (now + timedelta(days=20)).date(),
        ])
        self.assertEqual(levels["calls"], [(100.0, 350, 35), (105.0, 200, 20)])
        self.assertEqual(levels["puts"], [(95.0, 500, 50)])
        formatted = format_option_interest_levels(levels, 100)
        self.assertIn("콜 OI 집중: $100.00 (OI 350", formatted)
        self.assertIn("풋 OI 집중: $95.00 (OI 500", formatted)
        self.assertIn("당일 매수·매도 방향을 뜻하지 않습니다", formatted)

    def test_rsi_series_uses_wilder_smoothing_and_requires_warmup(self):
        closes = [float(value) for value in range(1, 31)]
        rsi_values = calculate_rsi_series(closes)

        self.assertTrue(all(value is None for value in rsi_values[:14]))
        self.assertTrue(all(value == 100.0 for value in rsi_values[14:]))
        self.assertEqual(
            calculate_rsi_series(closes[:14]),
            [None] * 14,
        )

    def test_detects_bullish_and_bearish_rsi_divergence_from_confirmed_swings(self):
        closes_for_bullish = [100.0] * 30
        closes_for_bullish[10] = 90.0
        closes_for_bullish[25] = 85.0
        bullish_rsi = [50.0] * 30
        bullish_rsi[10] = 30.0
        bullish_rsi[25] = 40.0

        closes_for_bearish = [100.0] * 30
        closes_for_bearish[10] = 110.0
        closes_for_bearish[25] = 115.0
        bearish_rsi = [50.0] * 30
        bearish_rsi[10] = 70.0
        bearish_rsi[25] = 60.0

        self.assertEqual(
            detect_rsi_divergence(closes_for_bullish, bullish_rsi),
            "상승 다이버전스",
        )
        self.assertEqual(
            detect_rsi_divergence_details(closes_for_bullish, bullish_rsi),
            ("상승 다이버전스", (10, 25)),
        )
        self.assertEqual(
            detect_rsi_divergence(closes_for_bearish, bearish_rsi),
            "하락 다이버전스",
        )
        self.assertEqual(
            detect_rsi_divergence(
                closes_for_bullish + [100.0] * 5,
                bullish_rsi + [50.0] * 5,
            ),
            "감지 없음",
        )

    def test_aggregates_hourly_closes_into_session_four_hour_bars(self):
        market_tz = ZoneInfo("America/New_York")
        hourly_bars = [
            (datetime(2026, 10, 19, 9, 30, tzinfo=market_tz) + timedelta(hours=index),
             float(index + 1))
            for index in range(7)
        ]

        self.assertEqual(
            aggregate_four_hour_closes(hourly_bars, "America/New_York"),
            [4.0, 7.0],
        )

    def test_weekly_closes_exclude_unfinished_week_before_friday_close(self):
        market_tz = ZoneInfo("America/New_York")
        daily_rows = [
            (100.0, 1000, 101.0, 99.0, 99.5,
             datetime(2026, 10, 15, 16, tzinfo=market_tz)),
            (101.0, 1000, 102.0, 100.0, 100.5,
             datetime(2026, 10, 16, 16, tzinfo=market_tz)),
            (102.0, 1000, 103.0, 101.0, 101.5,
             datetime(2026, 10, 19, 16, tzinfo=market_tz)),
        ]

        before_close = aggregate_weekly_closes(
            daily_rows,
            "America/New_York",
            datetime(2026, 10, 16, 15, 59, tzinfo=market_tz),
        )
        after_close = aggregate_weekly_closes(
            daily_rows,
            "America/New_York",
            datetime(2026, 10, 16, 16, 30, tzinfo=market_tz),
        )

        self.assertEqual(before_close, [])
        self.assertEqual(after_close, [101.0])

    def test_divergence_report_keeps_daily_rsi_and_adds_only_divergent_frames(self):
        market_tz = ZoneInfo("America/New_York")
        daily_rows = [
            (
                100.0 + index,
                1000,
                101.0 + index,
                99.0 + index,
                99.5 + index,
                datetime(2026, 6, 1, 16, tzinfo=market_tz)
                + timedelta(days=index),
            )
            for index in range(140)
        ]
        hourly_bars = [
            (
                datetime(2026, 6, 1, 9, 30, tzinfo=market_tz)
                + timedelta(days=day)
                + timedelta(hours=hour),
                float(day * 4 + hour + 1),
            )
            for day in range(140)
            for hour in range(4)
        ]
        with (
            patch(
                "stock_report.get_hourly_closes",
                return_value=(hourly_bars, "America/New_York"),
            ),
            patch(
                "stock_report.detect_rsi_divergence_details",
                side_effect=[
                    ("감지 없음", None),
                    ("상승 다이버전스", (16, 25)),
                    ("하락 다이버전스", (5, 15)),
                ],
            ),
        ):
            result = get_rsi_divergences("TSLA", daily_rows)

        self.assertIsInstance(result, RsiDivergenceReport)
        self.assertEqual(result.statuses["1일봉"], "감지 없음")
        self.assertEqual(result.statuses["4시간봉"], "상승 다이버전스")
        self.assertEqual(result.statuses["1주봉"], "하락 다이버전스")
        self.assertEqual(set(result.chart_series), {"1일봉", "4시간봉", "1주봉"})
        self.assertEqual(result.chart_series["4시간봉"]["pivots"][0][1], 100.0)
        self.assertEqual(result.chart_series["4시간봉"]["pivots"][1][1], 100.0)

    def test_formats_divergence_with_emphasis_and_timeframe_labels(self):
        message = format_rsi_divergences(
            {
                "4시간봉": "상승 다이버전스",
                "1일봉": "하락 다이버전스",
                "1주봉": "감지 없음",
            }
        )

        self.assertIn("🚨 RSI 다이버전스 감지 🚨", message)
        self.assertIn("4시간봉: 🟢 상승 다이버전스", message)
        self.assertIn("1일봉: 🔴 하락 다이버전스", message)
        self.assertIn("1주봉: — 감지 없음", message)

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
                patch(
                    "stock_report.create_chart",
                    side_effect=lambda ticker, *args: f"{ticker}.png",
                ) as create_chart,
                patch(
                    "stock_report.get_rsi_divergences",
                    return_value={
                        "4시간봉": "상승 다이버전스",
                        "1일봉": "감지 없음",
                        "1주봉": "감지 없음",
                    },
                ),
                patch("stock_report.get_news", return_value=[]),
                patch(
                    "stock_report.get_option_interest_levels",
                    return_value={
                        "expirations": [datetime(2026, 10, 23).date()],
                        "calls": [(105.0, 1200, 300)],
                        "puts": [(95.0, 900, 200)],
                    },
                ) as get_option_levels,
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
        self.assertIn("🚨 RSI 다이버전스 감지 🚨", korea_report)
        chart_divergences = [
            call.args[5] for call in create_chart.call_args_list
        ]
        self.assertEqual(
            [divergence["4시간봉"] for divergence in chart_divergences],
            ["상승 다이버전스"] * 2 + ["상승 다이버전스"] * 4,
        )
        chart_option_levels = [
            call.args[6] for call in create_chart.call_args_list
        ]
        self.assertEqual(
            chart_option_levels,
            [None, None]
            + [
                {
                    "expirations": [datetime(2026, 10, 23).date()],
                    "calls": [(105.0, 1200, 300)],
                    "puts": [(95.0, 900, 200)],
                }
            ] * 4,
        )
        self.assertNotIn("SK하이닉스 (000660.KS)", us_report)
        self.assertNotIn("NAVER (035420.KS)", us_report)
        self.assertEqual(
            [chart[0] for chart in us_charts], ["TSLA", "SPCX", "META", "SOXX"]
        )
        self.assertIn("개인 순매수 1.0억 원", korea_report)
        self.assertIn("외국인 순매도 2.0억 원", korea_report)
        self.assertIn("기관 중립 0.0억 원", korea_report)
        self.assertNotIn("[투자자별 수급", us_report)
        self.assertNotIn("[옵션 행사가별", korea_report)
        self.assertIn("[옵션 행사가별 미결제약정", us_report)
        self.assertIn("콜 OI 집중: $105.00", us_report)
        self.assertEqual(
            [call.args[0] for call in get_option_levels.call_args_list],
            ["TSLA", "SPCX", "META", "SOXX"],
        )
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
