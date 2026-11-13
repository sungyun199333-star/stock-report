import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from asset_report import (
    REPORT_SESSIONS,
    get_latest_price,
    is_us_market_close,
    make_report,
    parse_holdings,
)


class HoldingsTests(unittest.TestCase):
    def test_parses_holdings_without_rounding_quantities(self):
        holdings = parse_holdings(
            '[{"ticker":"TEST1.KS","name":"example","quantity":7},'
            '{"ticker":"TEST2.KQ","name":"another","quantity":2.5}]'
        )

        self.assertEqual(holdings[0]["quantity"], Decimal("7"))
        self.assertEqual(holdings[1]["quantity"], Decimal("2.5"))

    def test_rejects_duplicate_tickers(self):
        raw = (
            '[{"ticker":"TEST1.KS","name":"one","quantity":1},'
            '{"ticker":"TEST1.KS","name":"duplicate","quantity":2}]'
        )
        with self.assertRaisesRegex(ValueError, "중복"):
            parse_holdings(raw)

    def test_rejects_invalid_or_nonpositive_quantities(self):
        for quantity in ("0", "-1", "NaN", "not-a-number"):
            with self.subTest(quantity=quantity):
                raw = (
                    '[{"ticker":"TEST1.KS","name":"example",'
                    f'"quantity":"{quantity}"' + "}]"
                )
                with self.assertRaises(ValueError):
                    parse_holdings(raw)

    def test_formats_each_position_and_krw_total(self):
        holdings = parse_holdings(
            '[{"ticker":"TEST1.KS","name":"example","quantity":2},'
            '{"ticker":"TEST2.KQ","name":"another","quantity":3}]'
        )
        report = make_report(
            holdings,
            {
                "TEST1.KS": (Decimal("1000"), "KRW"),
                "TEST2.KQ": (Decimal("2"), "USD"),
            },
            (Decimal("1300"), "KRW"),
        )

        self.assertIn("💰 총 보유자산\n₩9,800", report)
        self.assertIn("🇰🇷 한국 주식 · 1종목", report)
        self.assertIn("🇺🇸 미국 주식 · 1종목", report)
        self.assertIn("소계  ₩2,000", report)
        self.assertIn("소계  ₩7,800", report)
        self.assertIn("₩2,000", report)
        self.assertIn("₩7,800", report)
        self.assertEqual(report.count("$1 = ₩1,300.00"), 1)
        self.assertIn("2주", report)
        self.assertIn("3주", report)
        self.assertIn("20.4%", report)
        self.assertIn("79.6%", report)

    @patch("asset_report.fetch")
    def test_reads_usd_regular_market_quote(self, mock_fetch):
        mock_fetch.return_value = (
            b'{"chart":{"result":[{"meta":{"currency":"USD",'
            b'"regularMarketPrice":123.45,"regularMarketTime":1794493800},'
            b'"timestamp":[],"indicators":{}}],"error":null}}'
        )

        price, currency = get_latest_price("TSLA")

        self.assertEqual(price, Decimal("123.45"))
        self.assertEqual(currency, "USD")

    def test_identifies_korea_and_us_market_close_triggers(self):
        self.assertEqual(REPORT_SESSIONS["30 6 * * 1-5"], "한국장 마감")
        self.assertEqual(REPORT_SESSIONS["0 20 * * 1-5"], "미국장 마감")
        self.assertEqual(REPORT_SESSIONS["0 21 * * 1-5"], "미국장 마감")

    def test_us_close_schedule_handles_daylight_saving_time(self):
        summer_close = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
        winter_close = datetime(2026, 1, 13, 21, 0, tzinfo=timezone.utc)
        summer_wrong_cron = datetime(2026, 7, 13, 21, 0, tzinfo=timezone.utc)
        winter_wrong_cron = datetime(2026, 1, 13, 20, 0, tzinfo=timezone.utc)

        self.assertTrue(is_us_market_close(summer_close))
        self.assertTrue(is_us_market_close(winter_close))
        self.assertFalse(is_us_market_close(summer_wrong_cron))
        self.assertFalse(is_us_market_close(winter_wrong_cron))


if __name__ == "__main__":
    unittest.main()
