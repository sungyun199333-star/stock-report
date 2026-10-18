#!/usr/bin/env python3
"""Send daily stock news and technical-indicator reports to Telegram."""

import html
import csv
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")
STOCKS = {
    "000660.KS": {"name": "SK하이닉스", "query": "SK하이닉스 OR SK hynix"},
    "TSLA": {"name": "Tesla", "query": "Tesla OR TSLA"},
    "SPCX": {"name": "SPAC and New Issue ETF", "query": '"SPAC and New Issue ETF" OR SPCX'},
    "META": {"name": "Meta Platforms", "query": "Meta Platforms OR META"},
}
MACRO_SERIES = {
    "DGS10": "미 10년물 금리",
    "DEXKOUS": "USD/KRW",
    "VIXCLS": "VIX",
    "DFEDTARU": "연준 목표금리 상단",
    "DFEDTARL": "연준 목표금리 하단",
    "CPIAUCSL": "CPI",
    "PCEPI": "PCE",
}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def strip_html(value):
    parser = TextExtractor()
    parser.feed(value)
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def translate_to_korean(text):
    if not text or re.search(r"[\uac00-\ud7a3]", text):
        return text
    if not re.search(r"[A-Za-z]", text):
        return text
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "ko", "dt": "t", "q": text}
    )
    try:
        response = json.loads(fetch(url))
        translated = "".join(segment[0] for segment in response[0] if segment[0])
    except (RuntimeError, TypeError, IndexError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"영문 뉴스의 한국어 번역에 실패했습니다: {exc}") from exc
    if not translated:
        raise RuntimeError("영문 뉴스의 한국어 번역 결과가 비어 있습니다.")
    return translated


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "DailyStockReport/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"데이터 요청 실패 ({url}): {exc}") from exc


def fetch_fred_series(series_id):
    start_date = (datetime.now(timezone.utc).date() - timedelta(days=500)).isoformat()
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode(
        {"id": series_id, "cosd": start_date}
    )
    reader = csv.DictReader(io.StringIO(fetch(url).decode("utf-8-sig")))
    observations = []
    for row in reader:
        raw_value = (row.get(series_id) or "").strip()
        if not raw_value or raw_value == ".":
            continue
        try:
            observations.append(
                (datetime.strptime(row["observation_date"], "%Y-%m-%d").date(),
                 float(raw_value))
            )
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"FRED {series_id} 응답을 해석하지 못했습니다.") from exc
    if not observations:
        raise RuntimeError(f"FRED {series_id}에 유효한 관측값이 없습니다.")
    return observations


def _weekly_change(observations):
    if len(observations) < 6:
        return None
    return observations[-1][1] - observations[-6][1]


def _inflation_yoy(observations):
    if len(observations) < 13:
        return None
    current_date, current = observations[-1]
    previous_date, previous_year = observations[-13]
    if previous_year == 0:
        return None
    previous_month_yoy = None
    if len(observations) >= 14 and observations[-14][1] != 0:
        previous_month_yoy = (observations[-2][1] / observations[-14][1] - 1) * 100
    return (
        current_date,
        (current / previous_year - 1) * 100,
        previous_month_yoy,
    )


def format_macro_summary(data, failures=()):
    alerts = []
    context = []

    treasury = data.get("DGS10", [])
    if treasury:
        latest_date, latest = treasury[-1]
        weekly_change = _weekly_change(treasury)
        if weekly_change is not None and abs(weekly_change) >= 0.10:
            direction = "상승" if weekly_change > 0 else "하락"
            alerts.append(f"미 10년물 {direction} {abs(weekly_change):.2f}%p/5거래일")
        context.append(f"미 10년물 {latest:.2f}% ({latest_date:%m-%d})")

    exchange = data.get("DEXKOUS", [])
    if exchange:
        latest_date, latest = exchange[-1]
        weekly_change = _weekly_change(exchange)
        if weekly_change is not None:
            base = latest - weekly_change
            percent_change = weekly_change / base * 100 if base else 0
            if abs(percent_change) >= 0.5:
                direction = "원화 약세" if percent_change > 0 else "원화 강세"
                alerts.append(f"USD/KRW {direction} {percent_change:+.2f}%/5거래일")
        context.append(f"USD/KRW {latest:,.1f} ({latest_date:%m-%d})")

    volatility = data.get("VIXCLS", [])
    if volatility:
        latest_date, latest = volatility[-1]
        weekly_change = _weekly_change(volatility)
        weekly_percent = (
            weekly_change / (latest - weekly_change) * 100
            if weekly_change is not None and latest != weekly_change else None
        )
        if latest >= 20:
            alerts.append(f"VIX {latest:.1f} (20 이상)")
        elif weekly_percent is not None and weekly_percent >= 10:
            alerts.append(f"VIX 주간 {weekly_percent:+.1f}% 급등")
        context.append(f"VIX {latest:.1f} ({latest_date:%m-%d})")

    upper = data.get("DFEDTARU", [])
    lower = data.get("DFEDTARL", [])
    if upper and lower:
        upper_change = _weekly_change(upper)
        lower_change = _weekly_change(lower)
        upper_date, upper_rate = upper[-1]
        _, lower_rate = lower[-1]
        if (
            upper_change is not None and lower_change is not None
            and (upper_change != 0 or lower_change != 0)
        ):
            alerts.append(f"연준 목표금리 변경: {lower_rate:.2f}~{upper_rate:.2f}%")
        context.append(f"연준 목표금리 {lower_rate:.2f}~{upper_rate:.2f}% ({upper_date:%m-%d})")

    inflation_context = []
    for series_id in ("CPIAUCSL", "PCEPI"):
        inflation = _inflation_yoy(data.get(series_id, []))
        if inflation is None:
            continue
        observation_date, yoy, previous_yoy = inflation
        item = f"{'CPI' if series_id == 'CPIAUCSL' else 'PCE'} 전년비 {yoy:+.1f}% ({observation_date:%Y-%m} 관측)"
        if previous_yoy is not None:
            change = yoy - previous_yoy
            item += f", 전월 관측 대비 {change:+.1f}%p"
            if abs(change) >= 0.2:
                alerts.append(
                    f"{'CPI' if series_id == 'CPIAUCSL' else 'PCE'} 물가상승률 "
                    f"{'가속' if change > 0 else '둔화'} {abs(change):.1f}%p"
                )
        inflation_context.append(item)

    if alerts:
        headline = "🔔 주요 매크로 업데이트: " + " · ".join(alerts)
    elif failures:
        headline = "⚠️ 매크로 일부 지표 미수신: 수신된 지표에서 기준 이상 변동 없음"
    else:
        headline = "✅ 주요 매크로 업데이트: 설정 기준 이상의 변동 없음"
    lines = [headline]
    if context:
        lines.append("시장 지표: " + " | ".join(context))
    if inflation_context:
        lines.append("물가 최신 관측: " + " | ".join(inflation_context))
    if failures:
        lines.append("⚠️ 일부 매크로 지표 조회 실패: " + ", ".join(failures))
    lines.append(
        "기준: 10년물 주간 ±0.10%p, USD/KRW ±0.5%, VIX ≥20 또는 주간 +10%, "
        "연준 목표금리 변경, CPI/PCE 전월 관측 대비 YoY ±0.2%p"
    )
    return "\n".join(lines)


def get_macro_summary():
    results = {}
    failures = []
    with ThreadPoolExecutor(max_workers=len(MACRO_SERIES)) as executor:
        futures = {
            executor.submit(fetch_fred_series, series_id): series_id
            for series_id in MACRO_SERIES
        }
        for future in as_completed(futures):
            series_id = futures[future]
            try:
                results[series_id] = future.result()
            except (RuntimeError, UnicodeDecodeError, csv.Error) as exc:
                print(f"FRED {series_id} 조회 실패: {exc}", file=sys.stderr)
                failures.append(MACRO_SERIES[series_id])
    return format_macro_summary(results, failures)


def get_prices(ticker):
    quote_url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?range=1y&interval=1d"
    )
    payload = json.loads(fetch(quote_url))
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(f"주가 데이터 오류: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("주가 데이터가 없습니다.")
    result = results[0]
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    rows = [
        (
            float(close), int(volume or 0), float(high), float(low), float(open_price),
            datetime.fromtimestamp(timestamp, timezone.utc).astimezone(SEOUL),
        )
        for timestamp, close, open_price, volume, high, low in zip(
            result.get("timestamp") or [], quote.get("close") or [], opens,
            quote.get("volume") or [], highs, lows
        )
        if close is not None and open_price is not None and high is not None and low is not None
    ]
    if len(rows) < 78:
        raise RuntimeError(
            f"{ticker} 분석 데이터가 부족합니다 "
            f"({len(rows)}일, 일목 구름대 계산에 최소 78일 필요)."
        )
    currency = result.get("meta", {}).get("currency", "통화 미상")
    return rows, currency


def midpoint(highs, lows, period, index):
    start = index - period + 1
    if start < 0:
        return None
    return (max(highs[start:index + 1]) + min(lows[start:index + 1])) / 2


def ema(values, period):
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(result[-1] + multiplier * (value - result[-1]))
    return result


def analyze(rows, currency):
    closes = [row[0] for row in rows]
    volumes = [row[1] for row in rows]
    highs = [row[2] for row in rows]
    lows = [row[3] for row in rows]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    avg_gain = sum(gains[:14]) / 14
    avg_loss = sum(losses[:14]) / 14
    for gain, loss in zip(gains[14:], losses[14:]):
        avg_gain = (13 * avg_gain + gain) / 14
        avg_loss = (13 * avg_loss + loss) / 14
    rsi = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    macd_values = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    signal_values = ema(macd_values, 9)
    price = closes[-1]
    moving_averages = {
        period: sum(closes[-period:]) / period if len(closes) >= period else None
        for period in (5, 20, 50, 120, 200)
    }
    sma20 = moving_averages[20]
    alignment_available = all(value is not None for value in moving_averages.values())
    bullish_alignment = (
        all(
            moving_averages[shorter] > moving_averages[longer]
            for shorter, longer in zip((5, 20, 50, 120), (20, 50, 120, 200))
        )
        if alignment_available else None
    )
    bearish_alignment = (
        all(
            moving_averages[shorter] < moving_averages[longer]
            for shorter, longer in zip((5, 20, 50, 120), (20, 50, 120, 200))
        )
        if alignment_available else None
    )
    alignment = (
        "데이터 부족 (200일 미만)" if not alignment_available
        else "정배열" if bullish_alignment
        else "역배열" if bearish_alignment
        else "혼조"
    )
    stddev20 = (sum((close - sma20) ** 2 for close in closes[-20:]) / 20) ** 0.5
    bollinger_upper = sma20 + 2 * stddev20
    bollinger_lower = sma20 - 2 * stddev20
    tenkan = midpoint(highs, lows, 9, len(rows) - 1)
    kijun = midpoint(highs, lows, 26, len(rows) - 1)
    cloud_index = len(rows) - 1 - 26
    cloud_tenkan = midpoint(highs, lows, 9, cloud_index)
    cloud_kijun = midpoint(highs, lows, 26, cloud_index)
    senkou_a = (cloud_tenkan + cloud_kijun) / 2
    senkou_b = midpoint(highs, lows, 52, cloud_index)
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)
    avg_volume = sum(volumes[-20:]) / 20
    macd, signal = macd_values[-1], signal_values[-1]
    positive_flags = (
        price > sma20,
        moving_averages[20] > moving_averages[50],
        macd > signal,
        40 <= rsi <= 70,
        price > bollinger_upper,
        price > cloud_top,
        bullish_alignment,
    )
    negative_flags = (
        price < sma20,
        moving_averages[20] < moving_averages[50],
        macd < signal,
        rsi > 70,
        price < bollinger_lower,
        price < cloud_bottom,
        bearish_alignment,
    )
    positive = sum(flag is True for flag in positive_flags)
    negative = sum(flag is True for flag in negative_flags)
    available_signals = sum(flag is not None for flag in positive_flags)

    def marker(is_positive, is_negative):
        if is_positive is None or is_negative is None:
            return "⚪"
        return "🟢" if is_positive else "🔴" if is_negative else "🟡"

    signal_lines = [
        f"{marker(positive_flags[0], negative_flags[0])} 종가 vs 20일선",
        f"{marker(positive_flags[1], negative_flags[1])} 20일선 vs 50일선",
        f"{marker(positive_flags[2], negative_flags[2])} MACD",
        f"{marker(positive_flags[3], negative_flags[3])} RSI(14)",
        f"{marker(positive_flags[4], negative_flags[4])} 볼린저 밴드 돌파",
        f"{marker(positive_flags[5], negative_flags[5])} 일목 구름대 위치",
        f"{marker(bullish_alignment, bearish_alignment)} 이동평균선 {alignment}",
    ]
    if positive >= 4 and negative <= 1:
        opinion = "매수 고려 신호: 상승 지표가 우세합니다. 한 번에 진입하기보다 분할 접근을 검토하세요."
    elif negative >= 4 and positive <= 1:
        opinion = "매도 또는 비중 축소 고려 신호: 하락·과열 지표가 우세합니다. 보유 목적과 위험 한도를 점검하세요."
    else:
        opinion = "보유·관망 신호: 지표가 엇갈리거나 충분히 정렬되지 않았습니다. 추가 확인을 기다리는 편이 낫습니다."
    if rsi < 30:
        opinion += " RSI 과매도 구간이므로 반등 가능성도 있어 RSI만으로 매도하지 마세요."
    opinion_marker = "🟢" if positive >= 4 and negative <= 1 else (
        "🔴" if negative >= 4 and positive <= 1 else "🟡"
    )
    return (
        f"종가: {price:,.2f} {currency} ({(price / closes[-2] - 1) * 100:+.2f}%)\n"
        f"RSI(14): {rsi:.1f} | MACD: {macd:,.1f} / 신호선 {signal:,.1f}\n"
        "이동평균선: "
        + " | ".join(
            f"{period}일 {moving_averages[period]:,.2f} {currency}"
            if moving_averages[period] is not None
            else f"{period}일 데이터 부족"
            for period in (5, 20, 50, 120, 200)
        )
        + f"\n배열 상태: {alignment} (5 > 20 > 50 > 120 > 200일선 순 정렬 기준)\n"
        f"볼린저 밴드(20, 2σ): 하단 {bollinger_lower:,.2f} {currency} | "
        f"중심 {sma20:,.2f} {currency} | 상단 {bollinger_upper:,.2f} {currency}\n"
        f"현재가 위치: {'상단 밴드 위' if price > bollinger_upper else '하단 밴드 아래' if price < bollinger_lower else '밴드 안'}\n"
        f"일목균형표: 전환선 {tenkan:,.2f} {currency} | 기준선 {kijun:,.2f} {currency}\n"
        f"현재 구름대(선행스팬 A/B): {cloud_bottom:,.2f}~{cloud_top:,.2f} {currency} "
        f"({'구름 위' if price > cloud_top else '구름 아래' if price < cloud_bottom else '구름 안'})\n"
        f"거래량: {volumes[-1]:,}주 (20일 평균 대비 "
        f"{volumes[-1] / avg_volume:.2f}배)\n"
        "지표별 신호:\n"
        + "\n".join(signal_lines)
        + f"\n집계: 🟢 상승 {positive}/{available_signals} | "
        + f"🔴 하락·과열 {negative}/{available_signals}\n"
        + f"참고 의견: {opinion_marker} {opinion}\n"
        + "신호 표시: 🟢 긍정 | 🔴 부정 | 🟡 중립 | ⚪ 데이터 부족\n"
        "※ 과거 가격 지표에 따른 일반 신호이며 개인별 투자 자문이나 수익 보장이 아닙니다. "
        "보유 단가·투자 기간·위험 감수도는 반영하지 않습니다."
    )


def get_news(ticker):
    stock = STOCKS[ticker]
    locale = ("ko", "KR", "KR:ko") if ticker.endswith(".KS") else ("en-US", "US", "US:en")
    news_url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {
            "q": f"{stock['query']} when:1d",
            "hl": locale[0],
            "gl": locale[1],
            "ceid": locale[2],
        }
    )
    root = ET.fromstring(fetch(news_url))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    articles = []
    for item in root.findall("./channel/item"):
        date_text = item.findtext("pubDate", default="")
        try:
            published = datetime.strptime(
                date_text, "%a, %d %b %Y %H:%M:%S %Z"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            published = None
        if published and published < cutoff:
            continue
        articles.append(
            (
                strip_html(item.findtext("title", default="")),
                strip_html(item.findtext("source", default="")),
                published.astimezone(SEOUL).strftime("%m-%d %H:%M")
                if published else "시간 미상",
                strip_html(item.findtext("description", default=""))[:300],
                item.findtext("link", default="").strip(),
            )
        )
    selected = articles[:5]
    return [
        (
            translate_to_korean(title),
            source,
            date,
            translate_to_korean(description),
            link,
        )
        for title, source, date, description, link in selected
    ]


def create_chart(ticker, stock, rows, currency, output_dir):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    closes = [row[0] for row in rows]
    volumes = [row[1] for row in rows]
    highs = [row[2] for row in rows]
    lows = [row[3] for row in rows]
    opens = [row[4] for row in rows]
    dates = [row[5] for row in rows]
    count = len(rows)
    x_values = list(range(count))
    averages = {
        period: [
            sum(closes[index - period + 1:index + 1]) / period
            if index + 1 >= period else None
            for index in range(count)
        ]
        for period in (5, 20, 50, 120, 200)
    }
    upper_band = []
    lower_band = []
    for index in range(count):
        if index < 19:
            upper_band.append(None)
            lower_band.append(None)
            continue
        window = closes[index - 19:index + 1]
        mean = sum(window) / 20
        deviation = (sum((value - mean) ** 2 for value in window) / 20) ** 0.5
        upper_band.append(mean + 2 * deviation)
        lower_band.append(mean - 2 * deviation)

    tenkan = [midpoint(highs, lows, 9, i) for i in range(count)]
    kijun = [midpoint(highs, lows, 26, i) for i in range(count)]
    span_a_raw = [
        (tenkan[i] + kijun[i]) / 2 if tenkan[i] is not None and kijun[i] is not None else None
        for i in range(count)
    ]
    span_b_raw = [midpoint(highs, lows, 52, i) for i in range(count)]
    span_a = [span_a_raw[i - 26] if i >= 26 else None for i in range(count)]
    span_b = [span_b_raw[i - 26] if i >= 26 else None for i in range(count)]

    start = max(0, count - 130)
    visible_x = x_values[start:]
    fig, (price_ax, volume_ax) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1]}, constrained_layout=True,
    )
    fig.suptitle(
        f"{ticker} - Daily Candles and Indicators | {dates[-1]:%Y-%m-%d}",
        fontsize=15, fontweight="bold",
    )
    for index in visible_x:
        is_up = closes[index] >= opens[index]
        color = "#159a69" if is_up else "#df4b52"
        price_ax.vlines(index, lows[index], highs[index], color=color, linewidth=1)
        bottom = min(opens[index], closes[index])
        height = max(abs(closes[index] - opens[index]), closes[index] * 0.0003)
        price_ax.add_patch(
            Rectangle((index - 0.3, bottom), 0.6, height, facecolor=color,
                      edgecolor=color, linewidth=0.7)
        )
        volume_ax.bar(index, volumes[index], color=color, width=0.65, alpha=0.75)

    ma_colors = {5: "#1f77b4", 20: "#ff9f1c", 50: "#8a5bb7",
                 120: "#555555", 200: "#a15c38"}
    for period, values in averages.items():
        price_ax.plot(
            x_values[start:], values[start:], color=ma_colors[period],
            linewidth=1.2, label=f"SMA {period}",
        )
    price_ax.plot(x_values[start:], upper_band[start:], color="#9878c7",
                  linewidth=0.8, linestyle="--", label="Bollinger (20, 2σ)")
    price_ax.plot(x_values[start:], lower_band[start:], color="#9878c7",
                  linewidth=0.8, linestyle="--")
    price_ax.fill_between(
        x_values[start:],
        [value if value is not None else float("nan") for value in lower_band[start:]],
        [value if value is not None else float("nan") for value in upper_band[start:]],
        color="#9878c7", alpha=0.08,
    )
    cloud_top = [
        max(a, b) if a is not None and b is not None else None
        for a, b in zip(span_a, span_b)
    ]
    cloud_bottom = [
        min(a, b) if a is not None and b is not None else None
        for a, b in zip(span_a, span_b)
    ]
    cloud_valid = [
        a is not None and b is not None
        for a, b in zip(cloud_bottom[start:], cloud_top[start:])
    ]
    price_ax.fill_between(
        x_values[start:],
        [value if value is not None else float("nan") for value in cloud_bottom[start:]],
        [value if value is not None else float("nan") for value in cloud_top[start:]],
        where=[valid and span_a[i] >= span_b[i] for i, valid in zip(visible_x, cloud_valid)],
        color="#52a879", alpha=0.18, label="Ichimoku cloud (bullish)",
    )
    price_ax.fill_between(
        x_values[start:],
        [value if value is not None else float("nan") for value in cloud_bottom[start:]],
        [value if value is not None else float("nan") for value in cloud_top[start:]],
        where=[valid and span_a[i] < span_b[i] for i, valid in zip(visible_x, cloud_valid)],
        color="#d76b72", alpha=0.16, label="Ichimoku cloud (bearish)",
    )
    price_ax.set_ylabel(f"Price ({currency})")
    volume_ax.set_ylabel("Volume")
    volume_ax.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _position: f"{value / 1_000_000:.1f}M")
    )
    volume_ax.set_xlabel("Date")
    price_ax.grid(True, alpha=0.2)
    volume_ax.grid(True, axis="y", alpha=0.2)
    price_ax.legend(loc="upper left", ncol=3, fontsize=8, framealpha=0.9)
    tick_positions = list(range(start, count, max(1, (count - start) // 6)))
    price_ax.set_xlim(start - 1, count)
    volume_ax.set_xticks(tick_positions)
    volume_ax.set_xticklabels(
        [dates[index].strftime("%b %d") for index in tick_positions],
        rotation=0,
    )
    chart_path = os.path.join(output_dir, f"{ticker.replace('.', '-')}.png")
    fig.savefig(chart_path, dpi=150, facecolor="white")
    plt.close(fig)
    return chart_path


def make_report(chart_dir):
    now = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"📊 보유 종목 일일 리포트 ({now} KST)",
        "",
        get_macro_summary(),
    ]
    charts = []
    for ticker, stock in STOCKS.items():
        rows, currency = get_prices(ticker)
        charts.append(
            (ticker, stock["name"], create_chart(ticker, stock, rows, currency, chart_dir))
        )
        lines.extend(["", f"━━ {stock['name']} ({ticker}) ━━", "[기술적 분석]",
                      analyze(rows, currency), "[최근 24시간 뉴스]"])
        try:
            news = get_news(ticker)
        except (RuntimeError, ET.ParseError) as exc:
            print(f"{ticker} 뉴스 조회 실패, 해당 종목 뉴스는 생략합니다: {exc}", file=sys.stderr)
            lines.append("뉴스 제공처에 연결할 수 없어 이 종목의 뉴스는 생략했습니다.")
            continue
        if not news:
            lines.append("최근 24시간 내 검색된 관련 뉴스가 없습니다.")
        for index, (title, source, date, description, link) in enumerate(news, 1):
            lines.append(
                f"{index}. {title}"
                + (f" · {source}" if source else "")
                + f" ({date} KST)"
            )
            if description and description != title:
                lines.append(description)
            if link:
                lines.append(f"[[ARTICLE_LINK:{link}]]")
    lines.extend([
        "",
        "매크로: FRED | 뉴스: Google News RSS | 번역: Google Translate | 시세: Yahoo Finance",
        "※ 번역은 자동 처리되어 오역이 있을 수 있습니다. 원문은 링크에서 확인하세요.",
    ])
    return "\n".join(lines), charts


def telegram_html(message):
    link_pattern = re.compile(r"\[\[ARTICLE_LINK:(https?://[^\]\s]+)\]\]")
    parts = []
    position = 0
    for match in link_pattern.finditer(message):
        parts.append(html.escape(message[position:match.start()]))
        url = html.escape(match.group(1), quote=True)
        parts.append(f'<a href="{url}">🔗 기사 원문</a>')
        position = match.end()
    parts.append(html.escape(message[position:]))
    return "".join(parts)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID 환경 변수가 필요합니다.")
    chunks = []
    current = ""
    for line in message.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > 3500 and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunks:
        data = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": telegram_html(chunk),
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        request = urllib.request.Request(endpoint, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                details = json.loads(exc.read()).get("description", str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                details = str(exc)
            raise RuntimeError(
                f"텔레그램이 메시지 요청을 거부했습니다 (HTTP {exc.code}): {details}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"텔레그램 전송 실패: {exc}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"텔레그램 API 오류: {result}")


def send_chart(ticker, name, image_path):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID 환경 변수가 필요합니다.")
    boundary = "----StockReportBoundary7MA4YWxkTrZu0gW"
    fields = {
        "chat_id": chat_id,
        "caption": f"{name} ({ticker}) 일봉 차트 | 캔들·이평선·볼린저 밴드·일목 구름대",
    }
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        )
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="photo"; filename="{os.path.basename(image_path)}"\r\n'.encode()
    )
    body.extend(b"Content-Type: image/png\r\n\r\n")
    with open(image_path, "rb") as image_file:
        body.extend(image_file.read())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            details = json.loads(exc.read()).get("description", str(exc))
        except (json.JSONDecodeError, UnicodeDecodeError):
            details = str(exc)
        raise RuntimeError(
            f"텔레그램 차트 전송 실패 (HTTP {exc.code}): {details}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"텔레그램 차트 전송 실패: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"텔레그램 차트 API 오류: {result}")


def main():
    try:
        if "--dry-run" in sys.argv[1:]:
            chart_dir = os.path.join(os.getcwd(), "stock_chart_preview")
            os.makedirs(chart_dir, exist_ok=True)
            temporary_charts = None
        else:
            temporary_charts = tempfile.TemporaryDirectory(prefix="stock-report-charts-")
            chart_dir = temporary_charts.name
        try:
            report, charts = make_report(chart_dir)
            if "--dry-run" in sys.argv[1:]:
                print(
                    re.sub(
                        r"\[\[ARTICLE_LINK:(https?://[^\]\s]+)\]\]",
                        r"🔗 기사 원문: \1",
                        report,
                    )
                )
                for ticker, _name, chart_path in charts:
                    print(f"{ticker} 차트 미리보기 파일: {chart_path}")
                return 0
            send_telegram(report)
            for ticker, name, chart_path in charts:
                send_chart(ticker, name, chart_path)
        finally:
            if temporary_charts is not None:
                temporary_charts.cleanup()
    except (RuntimeError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"리포트 실패: {exc}", file=sys.stderr)
        return 1
    print("리포트를 텔레그램으로 전송했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
