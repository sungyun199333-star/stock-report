#!/usr/bin/env python3
"""Send a daily KRW valuation report for configured stock holdings."""

import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_report import fetch, send_telegram

SEOUL = ZoneInfo("Asia/Seoul")
TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]+$")


def parse_holdings(raw):
    try:
        holdings = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ASSET_HOLDINGS_JSON이 올바른 JSON이 아닙니다: {exc}") from exc
    if not isinstance(holdings, list) or not holdings:
        raise ValueError("ASSET_HOLDINGS_JSON은 1개 이상의 보유 종목 배열이어야 합니다.")

    parsed = []
    seen_tickers = set()
    for index, item in enumerate(holdings, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"보유 종목 {index}번은 JSON 객체여야 합니다.")
        ticker = str(item.get("ticker", "")).strip().upper()
        name = str(item.get("name", "")).strip()
        if not ticker or not TICKER_PATTERN.fullmatch(ticker):
            raise ValueError(f"보유 종목 {index}번의 ticker 형식이 올바르지 않습니다.")
        if not name:
            raise ValueError(f"{ticker}의 name 값이 비어 있습니다.")
        if ticker in seen_tickers:
            raise ValueError(f"중복된 종목 코드가 있습니다: {ticker}")
        try:
            quantity = Decimal(str(item.get("quantity", "")))
        except InvalidOperation as exc:
            raise ValueError(f"{ticker}의 보유 수량이 숫자가 아닙니다.") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError(f"{ticker}의 보유 수량은 0보다 큰 숫자여야 합니다.")
        parsed.append({"ticker": ticker, "name": name, "quantity": quantity})
        seen_tickers.add(ticker)
    return parsed


def get_latest_price(ticker):
    encoded_ticker = urllib.parse.quote(ticker, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_ticker}"
        "?range=5d&interval=1d"
    )
    try:
        payload = json.loads(fetch(url))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{ticker} 시세 응답을 해석할 수 없습니다.") from exc

    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"{ticker} 시세 조회 오류: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"{ticker} 시세 데이터가 없습니다.")
    result = results[0]
    currency = result.get("meta", {}).get("currency")
    if currency not in {"KRW", "USD"}:
        raise RuntimeError(
            f"{ticker} 시세 통화가 {currency or '미상'}입니다. "
            "현재 리포트는 원화(KRW)와 미국 달러(USD) 종목을 지원합니다."
        )

    metadata = result.get("meta", {})
    price = metadata.get("regularMarketPrice")
    timestamp = metadata.get("regularMarketTime")
    if price is None or timestamp is None:
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        timestamps = result.get("timestamp") or []
        closes = quote.get("close") or []
        valid_rows = [
            (row_timestamp, close)
            for row_timestamp, close in zip(timestamps, closes)
            if close is not None
        ]
        if not valid_rows:
            raise RuntimeError(f"{ticker}의 유효한 시세가 없습니다.")
        timestamp, price = valid_rows[-1]
    quote_time = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(SEOUL)
    return Decimal(str(price)), quote_time, currency


def make_report(holdings, quotes, usd_krw=None):
    total = Decimal(0)
    lines = ["📊 보유 주식 일일 평가 리포트"]
    for holding in holdings:
        ticker = holding["ticker"]
        if ticker not in quotes:
            raise ValueError(f"{ticker}의 시세 데이터가 없습니다.")
        price, quote_time, currency = quotes[ticker]
        if currency == "USD":
            if usd_krw is None:
                raise ValueError("미국 주식 평가에 필요한 USD/KRW 환율이 없습니다.")
            market_value = price * holding["quantity"] * usd_krw[0]
            local_price = f"${price:,.2f}"
            quote_label = f"{quote_time:%m-%d %H:%M} KST"
        else:
            market_value = price * holding["quantity"]
            local_price = f"₩{price:,.0f}"
            quote_label = f"{quote_time:%Y-%m-%d %H:%M} KST"
        total += market_value
        quantity = f"{holding['quantity']:,.4f}".rstrip("0").rstrip(".")
        lines.append(
            f"• {holding['name']} {quantity}주 × {local_price}"
            f" = ₩{market_value:,.0f} ({quote_label})"
        )
    lines.extend(["", f"총 평가액: ₩{total:,.0f}"])
    if usd_krw is not None:
        fx_rate, fx_time = usd_krw
        lines.append(f"환율: $1 = ₩{fx_rate:,.2f} ({fx_time:%m-%d %H:%M} KST)")
    lines.append(
        "가격 기준: 최신 정규장 시세 | Yahoo Finance "
        "| 수수료·세금·현금은 반영하지 않았습니다."
    )
    return "\n".join(lines)


def main():
    try:
        raw_holdings = os.environ.get("ASSET_HOLDINGS_JSON")
        if not raw_holdings:
            raise RuntimeError(
                "GitHub Actions Secret ASSET_HOLDINGS_JSON을 설정해야 합니다."
            )
        holdings = parse_holdings(raw_holdings)
        quotes = {
            holding["ticker"]: get_latest_price(holding["ticker"])
            for holding in holdings
        }
        usd_krw = (
            get_latest_price("KRW=X")[:2]
            if any(quote[2] == "USD" for quote in quotes.values())
            else None
        )
        send_telegram(make_report(holdings, quotes, usd_krw))
    except (RuntimeError, ValueError) as exc:
        print(f"자산 리포트 실패: {exc}", file=sys.stderr)
        return 1
    print("자산 리포트를 텔레그램으로 전송했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
