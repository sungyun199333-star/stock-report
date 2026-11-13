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
REPORT_SESSIONS = {
    "30 6 * * 1-5": "한국장 마감",
    "0 20 * * 1-5": "미국장 마감",
    "0 21 * * 1-5": "미국장 마감",
}
US_EASTERN = ZoneInfo("America/New_York")


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


def is_us_market_close(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(US_EASTERN).strftime("%H:%M") == "16:00"


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
    if price is None:
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close") or []
        valid_closes = [close for close in closes if close is not None]
        if not valid_closes:
            raise RuntimeError(f"{ticker}의 유효한 시세가 없습니다.")
        price = valid_closes[-1]
    return Decimal(str(price)), currency


def make_report(holdings, quotes, usd_krw=None, session_label=""):
    groups = {"KRW": [], "USD": []}
    for holding in holdings:
        ticker = holding["ticker"]
        if ticker not in quotes:
            raise ValueError(f"{ticker}의 시세 데이터가 없습니다.")
        price, currency = quotes[ticker]
        if currency == "USD":
            if usd_krw is None:
                raise ValueError("미국 주식 평가에 필요한 USD/KRW 환율이 없습니다.")
            market_value = price * holding["quantity"] * usd_krw[0]
            local_price = f"${price:,.2f}"
        else:
            market_value = price * holding["quantity"]
            local_price = f"₩{price:,.0f}"
        groups[currency].append((holding, price, local_price, market_value))

    total = sum(
        (row[3] for currency_rows in groups.values() for row in currency_rows),
        Decimal(0),
    )
    report_date = datetime.now(SEOUL).strftime("%Y-%m-%d")
    title = f"📊 자산 평가 리포트 · {report_date}"
    if session_label:
        title += f" · {session_label}"
    lines = [
        title,
        "",
        "💰 총 보유자산",
        f"₩{total:,.0f}",
    ]

    for currency, label in (("KRW", "🇰🇷 한국 주식"), ("USD", "🇺🇸 미국 주식")):
        rows = groups[currency]
        if not rows:
            continue
        subtotal = sum((row[3] for row in rows), Decimal(0))
        lines.extend(["", f"{label} · {len(rows)}종목", f"소계  ₩{subtotal:,.0f}"])
        for holding, _price, local_price, market_value in rows:
            weight = market_value / total * Decimal(100) if total else Decimal(0)
            quantity = f"{holding['quantity']:,.4f}".rstrip("0").rstrip(".")
            lines.extend([
                f"• {holding['name']} · {quantity}주",
                f"  {local_price}/주 → ₩{market_value:,.0f} · {weight:.1f}%",
            ])

    if usd_krw is not None:
        fx_rate, _fx_currency = usd_krw
        lines.extend(["", f"💱 미국 주식 환산 환율  $1 = ₩{fx_rate:,.2f}"])
    lines.extend([
        "",
        "시세: 각 시장의 최신 정규장 기준 · Yahoo Finance",
        "수수료·세금·현금 미포함",
        f"📌 전체 주식금액 합계: ₩{total:,.0f}",
    ])
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
            get_latest_price("KRW=X")
            if any(quote[1] == "USD" for quote in quotes.values())
            else None
        )
        if usd_krw is not None and usd_krw[1] != "KRW":
            raise RuntimeError("USD/KRW 환율 시세의 통화가 올바르지 않습니다.")
        schedule = os.environ.get("ASSET_REPORT_SESSION", "")
        session_label = REPORT_SESSIONS.get(schedule, schedule or "수동 실행")
        send_telegram(make_report(holdings, quotes, usd_krw, session_label))
    except (RuntimeError, ValueError) as exc:
        print(f"자산 리포트 실패: {exc}", file=sys.stderr)
        return 1
    print("자산 리포트를 텔레그램으로 전송했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
