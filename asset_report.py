#!/usr/bin/env python3
"""Send a daily total-asset change report from Google Sheets to Telegram."""

import json
import os
import sys
import urllib.parse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from stock_report import send_telegram

DATE_HEADERS = {"날짜", "일자", "기준일", "date"}
VALUE_HEADERS = {
    "평가액", "총평가액", "평가금액", "총액(원)", "total_value", "valuation"
}
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def normalize_header(value):
    return "".join(str(value).strip().lower().split())


def parse_date(value):
    text = str(value).strip()
    if not text:
        raise ValueError("날짜 값이 비어 있습니다.")
    iso_text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        pass
    for date_format in (
        "%Y. %m. %d.",
        "%Y.%m.%d.",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise ValueError(f"날짜 형식을 읽을 수 없습니다: {text}")


def parse_amount(value):
    text = str(value).strip().replace(",", "").replace("원", "").replace("₩", "")
    text = text.replace("−", "-").replace(" ", "")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"평가액을 숫자로 읽을 수 없습니다: {value}") from exc
    if not amount.is_finite():
        raise ValueError(f"평가액은 유한한 숫자여야 합니다: {value}")
    return amount


def parse_history(values, date_column=None, value_column=None):
    if not values or not values[0]:
        raise ValueError("스프레드시트에서 헤더 행을 찾지 못했습니다.")

    headers = [normalize_header(cell) for cell in values[0]]
    date_name = normalize_header(date_column) if date_column else None
    value_name = normalize_header(value_column) if value_column else None
    if date_name:
        date_index = headers.index(date_name) if date_name in headers else None
    else:
        date_index = next(
            (index for index, header in enumerate(headers) if header in DATE_HEADERS),
            None,
        )
    if value_name:
        value_index = headers.index(value_name) if value_name in headers else None
    else:
        value_index = next(
            (index for index, header in enumerate(headers) if header in VALUE_HEADERS),
            None,
        )
    if date_index is None or value_index is None:
        raise ValueError(
            "날짜/평가액 열을 찾지 못했습니다. "
            f"현재 헤더: {', '.join(str(cell) for cell in values[0])}"
        )

    totals = defaultdict(Decimal)
    for row_number, row in enumerate(values[1:], start=2):
        date_value = row[date_index] if date_index < len(row) else ""
        amount_value = row[value_index] if value_index < len(row) else ""
        if not str(date_value).strip() and not str(amount_value).strip():
            continue
        try:
            if not str(date_value).strip():
                raise ValueError("날짜 값이 비어 있습니다.")
            if not str(amount_value).strip():
                raise ValueError("평가액이 비어 있습니다.")
            row_date = parse_date(date_value)
            totals[row_date] += parse_amount(amount_value)
        except ValueError as exc:
            raise ValueError(f"{row_number}행 처리 실패: {exc}") from exc

    if len(totals) < 2:
        raise ValueError("전일 대비 비교를 위해 날짜가 다른 기록이 최소 2개 필요합니다.")
    return dict(sorted(totals.items()))


def make_report(history, currency="원"):
    (previous_date, previous_total), (latest_date, latest_total) = list(history.items())[-2:]
    change = latest_total - previous_total
    change_percent = (
        change / previous_total * Decimal(100) if previous_total else None
    )
    percent_text = f"{change_percent:+.2f}%" if change_percent is not None else "계산 불가"
    return (
        f"📊 자산 평가액 일일 리포트\n"
        f"기준일: {latest_date.isoformat()}\n"
        f"총 평가액: {latest_total:,.0f} {currency}\n"
        f"변동: {previous_date.isoformat()} 대비 "
        f"{change:+,.0f} {currency} ({percent_text})"
    )


def get_sheet_values():
    credentials_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID")
    sheet_name = os.environ.get("ASSET_SHEET_NAME", "Sheet1")
    if not credentials_json or not spreadsheet_id:
        raise RuntimeError(
            "GOOGLE_SHEETS_CREDENTIALS와 GOOGLE_SHEET_ID 설정이 필요합니다."
        )

    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.auth.exceptions import GoogleAuthError
        from google.oauth2.service_account import Credentials
        from requests.exceptions import RequestException
    except ImportError as exc:
        raise RuntimeError("Google Sheets 연결을 위해 google-auth 패키지가 필요합니다.") from exc

    try:
        service_account_info = json.loads(credentials_json)
        credentials = Credentials.from_service_account_info(
            service_account_info, scopes=[SHEETS_SCOPE]
        )
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Google 서비스 계정 인증 정보가 올바르지 않습니다: {exc}") from exc

    escaped_sheet_name = sheet_name.replace("'", "''")
    cell_range = f"'{escaped_sheet_name}'!A:ZZ"
    endpoint = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{urllib.parse.quote(spreadsheet_id, safe='')}/values/"
        f"{urllib.parse.quote(cell_range, safe='')}"
    )
    try:
        response = AuthorizedSession(credentials).get(endpoint, timeout=30)
    except GoogleAuthError as exc:
        raise RuntimeError(f"Google Sheets 인증에 실패했습니다: {exc}") from exc
    except RequestException as exc:
        raise RuntimeError(f"Google Sheets 요청에 실패했습니다: {exc}") from exc
    if not response.ok:
        try:
            details = response.json().get("error", {}).get("message", "알 수 없는 오류")
        except ValueError:
            details = "응답을 해석할 수 없습니다."
        raise RuntimeError(
            f"Google Sheets 조회 실패 (HTTP {response.status_code}): {details}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Google Sheets 응답이 올바른 JSON이 아닙니다.") from exc
    rows = payload.get("values")
    if not rows:
        raise RuntimeError(f"'{sheet_name}' 시트에 읽을 데이터가 없습니다.")
    return rows


def main():
    try:
        values = get_sheet_values()
        history = parse_history(
            values,
            date_column=os.environ.get("ASSET_DATE_COLUMN"),
            value_column=os.environ.get("ASSET_VALUE_COLUMN"),
        )
        message = make_report(history, os.environ.get("ASSET_CURRENCY", "원"))
        send_telegram(message)
    except (RuntimeError, ValueError) as exc:
        print(f"자산 리포트 실패: {exc}", file=sys.stderr)
        return 1
    print("자산 리포트를 텔레그램으로 전송했습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
