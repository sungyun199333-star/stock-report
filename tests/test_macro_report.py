import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from stock_report import (
    fetch_cnn_fear_greed_index,
    fetch_fred_series,
    fear_greed_label,
    format_macro_summary,
)


class MacroSummaryTests(unittest.TestCase):
    def test_highlights_selected_sensitive_thresholds_and_latest_inflation(self):
        daily_dates = [date(2026, 10, day) for day in range(1, 7)]
        macro_data = {
            "DGS10": list(zip(daily_dates, [4.00, 4.01, 4.02, 4.03, 4.04, 4.15])),
            "DEXKOUS": list(zip(daily_dates, [1400, 1401, 1400, 1402, 1401, 1408])),
            "VIXCLS": list(zip(daily_dates, [17, 18, 17, 18, 19, 20.5])),
            "DFEDTARU": list(zip(daily_dates, [3.75, 3.75, 3.75, 3.75, 3.75, 3.50])),
            "DFEDTARL": list(zip(daily_dates, [3.50, 3.50, 3.50, 3.50, 3.50, 3.25])),
        }
        monthly_dates = [date(2025 + (month - 1) // 12, (month - 1) % 12 + 1, 1)
                         for month in range(11, 25)]
        monthly_values = [100 + index * 0.3 for index in range(14)]
        monthly_values[-1] += 1
        macro_data["CPIAUCSL"] = list(zip(monthly_dates, monthly_values))
        macro_data["PCEPI"] = list(zip(monthly_dates, monthly_values))

        summary = format_macro_summary(
            macro_data,
            fear_greed={
                "score": 18,
                "observed_at": datetime(2026, 10, 6, 16, 0),
            },
        )

        self.assertIn("미 10년물 상승 0.15%p/5거래일", summary)
        self.assertIn("USD/KRW 원화 약세", summary)
        self.assertIn("VIX 20.5 (20 이상)", summary)
        self.assertIn("CNN 공포·탐욕지수 18.0/100 (극단적 공포)", summary)
        self.assertIn("연준 목표금리 변경: 3.25~3.50%", summary)
        self.assertIn("CPI 전년비", summary)
        self.assertIn("PCE 전년비", summary)
        self.assertIn("물가상승률 가속", summary)

    def test_reports_no_threshold_break_and_source_failures(self):
        dates = [date(2026, 10, day) for day in range(1, 7)]
        stable = list(zip(dates, [4.0] * 6))

        summary = format_macro_summary(
            {
                "DGS10": stable,
                "DEXKOUS": list(zip(dates, [1400] * 6)),
                "VIXCLS": list(zip(dates, [15] * 6)),
                "DFEDTARU": list(zip(dates, [3.75] * 6)),
                "DFEDTARL": list(zip(dates, [3.50] * 6)),
            },
            failures=["CPI"],
        )

        self.assertIn("수신된 지표에서 기준 이상 변동 없음", summary)
        self.assertIn("일부 매크로 지표 조회 실패: CPI", summary)

    def test_parses_fred_csv_and_ignores_missing_observations(self):
        csv_data = (
            b"observation_date,DGS10\n"
            b"2026-10-01,4.00\n"
            b"2026-10-02,.\n"
            b"2026-10-05,4.10\n"
        )
        with patch("stock_report.fetch", return_value=csv_data):
            observations = fetch_fred_series("DGS10")

        self.assertEqual(
            observations,
            [(date(2026, 10, 1), 4.0), (date(2026, 10, 5), 4.1)],
        )

    def test_parses_cnn_fear_greed_response(self):
        payload = (
            b'{"fear_and_greed":{"score":72.4,"rating":"greed",'
            b'"timestamp":"2026-10-16T16:00:00-04:00"}}'
        )
        with patch("stock_report.fetch", return_value=payload):
            result = fetch_cnn_fear_greed_index(
                datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))
            )

        self.assertEqual(result["score"], 72.4)
        self.assertEqual(result["observed_at"].date(), date(2026, 10, 17))
        self.assertEqual(fear_greed_label(result["score"]), "탐욕")

    def test_fear_greed_summary_reports_source_failure(self):
        summary = format_macro_summary(
            {},
            failures=["CNN 공포·탐욕지수"],
        )

        self.assertIn("CNN 공포·탐욕지수 조회 실패", summary)
        self.assertIn("일부 매크로 지표 조회 실패", summary)

    def test_non_extreme_fear_greed_is_context_not_alert(self):
        summary = format_macro_summary(
            {},
            fear_greed={"score": 62, "observed_at": None},
        )

        self.assertIn("CNN 공포·탐욕지수 62.0/100 (탐욕)", summary)
        self.assertIn("설정 기준 이상의 변동 없음", summary)
        self.assertNotIn("주요 매크로 업데이트: CNN 공포·탐욕지수", summary)


if __name__ == "__main__":
    unittest.main()
