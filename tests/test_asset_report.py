import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from asset_report import get_latest_price, make_report, parse_holdings


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
                "TEST1.KS": (
                    Decimal("1000"),
                    datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc),
                    "KRW",
                ),
                "TEST2.KQ": (
                    Decimal("2"),
                    datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc),
                    "USD",
                ),
            },
            (Decimal("1300"), datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)),
        )

        self.assertIn("총 평가액: ₩9,800", report)
        self.assertIn("₩2,000", report)
        self.assertIn("₩7,800", report)
        self.assertIn("$1 = ₩1,300.00", report)

    @patch("asset_report.fetch")
    def test_reads_usd_regular_market_quote(self, mock_fetch):
        mock_fetch.return_value = (
            b'{"chart":{"result":[{"meta":{"currency":"USD",'
            b'"regularMarketPrice":123.45,"regularMarketTime":1794493800},'
            b'"timestamp":[],"indicators":{}}],"error":null}}'
        )

        price, quote_time, currency = get_latest_price("TSLA")

        self.assertEqual(price, Decimal("123.45"))
        self.assertEqual(currency, "USD")
        self.assertEqual(str(quote_time.tzinfo), "Asia/Seoul")


if __name__ == "__main__":
    unittest.main()
