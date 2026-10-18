# Daily Google Sheets asset report

The `Daily asset Telegram report` workflow reads dated valuation rows from a
Google Sheet and sends the change between the two most recent dates to Telegram
every day at 06:00 Korea Standard Time. It can also be run manually from the
GitHub Actions tab.

## Sheet format

The default tab is `Sheet1`. Its first row must contain `날짜` and `총액(원)`
headers, for example:

| 날짜 | 총액(원) |
| --- | ---: |
| 2026-10-16 | 12500000 |
| 2026-10-17 | 12620000 |

All amounts should use the same currency and include any desired conversion
before they are recorded. The workflow adds all valuation rows that share the
same date. Use either one total row per date or one row per asset per date; do
not include both a daily total and its constituent asset rows, as that would
double-count the total.
Dates should be formatted as `YYYY-MM-DD`. Values may include comma grouping
and a `원`/`₩` suffix. The report compares the latest two dates found in the
sheet, which need not be yesterday and today.

If the existing tab or headers have different names, set the corresponding
repository Actions variables: `ASSET_SHEET_NAME`, `ASSET_DATE_COLUMN`,
`ASSET_VALUE_COLUMN`, and optionally `ASSET_CURRENCY`.

## Google Sheets access

1. Create a Google Cloud service account and enable the Google Sheets API.
2. Share the spreadsheet with the service account's email as a **Viewer**.
3. Store the service account JSON key as the repository Actions secret
   `GOOGLE_SHEETS_CREDENTIALS` (the complete JSON content).
4. Store the spreadsheet ID from its URL as the Actions secret
   `GOOGLE_SHEET_ID`.

The integration requests read-only Sheets access. Do not put the service
account key, spreadsheet ID, or Telegram bot token in source code.

## Telegram and scheduled run

Add or verify these repository Actions secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The schedule is `0 21 * * *` in UTC, which corresponds to 06:00 the next day
in Korea Standard Time. GitHub Actions can delay scheduled starts, and
scheduled workflows run from the repository's default branch. To test delivery,
use **Actions → Daily asset Telegram report → Run workflow** after adding the
secrets and variables.
