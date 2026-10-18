#!/usr/bin/env python3
"""Send daily stock news and technical-indicator reports to Telegram."""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    rows = [
        (float(close), int(volume or 0), float(high), float(low))
        for close, volume, high, low in zip(
            quote.get("close") or [], quote.get("volume") or [], highs, lows
        )
        if close is not None and high is not None and low is not None
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


def make_report():
    now = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 보유 종목 일일 리포트 ({now} KST)"]
    for ticker, stock in STOCKS.items():
        rows, currency = get_prices(ticker)
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
        "뉴스: Google News RSS | 번역: Google Translate | 시세: Yahoo Finance",
        "※ 번역은 자동 처리되어 오역이 있을 수 있습니다. 원문은 링크에서 확인하세요.",
    ])
    return "\n".join(lines)


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


def main():
    try:
        report = make_report()
        if "--dry-run" in sys.argv[1:]:
            print(
                re.sub(
                    r"\[\[ARTICLE_LINK:(https?://[^\]\s]+)\]\]",
                    r"🔗 기사 원문: \1",
                    report,
                )
            )
            return 0
        send_telegram(report)
    except (RuntimeError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"리포트 실패: {exc}", file=sys.stderr)
        return 1
    print("리포트를 텔레그램으로 전송했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
