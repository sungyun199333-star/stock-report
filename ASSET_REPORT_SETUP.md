# Daily stock valuation report

The `Daily asset Telegram report` workflow fetches the latest available daily
regular-session prices for the configured holdings, converts USD-denominated
positions to KRW using the USD/KRW rate, calculates each position's market
value and the portfolio total, then sends the report to Telegram after both
market closes on weekdays. It can also be started manually in GitHub Actions.

## Configure holdings privately

This repository is public. Do not put personal holdings or quantities in source
files, workflow variables, or commits. Store the complete holdings JSON as the
repository Actions secret `ASSET_HOLDINGS_JSON`.

In the repository, open **Settings → Secrets and variables → Actions → New
repository secret**. Set the name to `ASSET_HOLDINGS_JSON` and the value to a
JSON array in this format:

```json
[
  {"ticker": "KRX_CODE.KS", "name": "표시할 종목명", "quantity": 1}
]
```

Use Yahoo Finance ticker symbols, including `.KS` for KOSPI and `.KQ` for
KOSDAQ listings. U.S. stock symbols are used without a suffix. The quantity
can be fractional. Edit this secret whenever holdings or quantities change.

## Required Telegram secrets

Add or verify these repository Actions secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Never put tokens or private account details in source code or commit them.

## Price and valuation details

The report uses the latest available regular-session quote from Yahoo Finance.
KRW quotes are included directly; USD quotes are converted using Yahoo
Finance's USD/KRW rate. On weekends and market holidays, the latest available
quotes will be reported. The report shows each holding's KRW value and the
portfolio total. Telegram messages put the total first, then show Korean and
U.S. holdings in separate groups with group subtotals and each holding's share
of the portfolio. The USD/KRW rate appears once at the bottom, not alongside
each U.S. holding. Commissions, taxes, and cash balances are not included.
The final line repeats the total stock value for quick reference.

## Market-close schedule

The workflow requests runs at these UTC times on weekdays:

- Korea Exchange close: `30 6 * * 1-5` UTC = 15:30 Korea Standard Time.
- U.S. market close in daylight time: `0 20 * * 1-5` UTC = 16:00 Eastern
  Daylight Time (05:00 the next day in Korea).
- U.S. market close in standard time: `0 21 * * 1-5` UTC = 16:00 Eastern
  Standard Time (06:00 the next day in Korea).

The U.S. workflow checks the current `America/New_York` hour and sends only
the schedule corresponding to 16:00 local time, so daylight-saving changes do
not cause a second or wrongly timed U.S. report. Manual runs are not gated.

GitHub Actions scheduled starts are best-effort and can be delayed; they cannot
guarantee execution at the exact exchange closing second. The schedules are
weekdays, not exchange-holiday calendars, so on a market holiday the latest
available quote may be from the prior trading session. Scheduled workflows run
from the repository's default branch. To test, use
**Actions → Daily asset Telegram report → Run workflow**.
