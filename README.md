# Financial Tracker

A private, local web app that turns a household's monthly bank, card, savings and investment statements into one clear monthly report: where the money came from, where it went, what was saved and what was invested.

Everything runs on your own Mac. Statements and results never leave it. The only exception is the pieces Claude reads to help (see [Privacy](#privacy)).

## Features
- **Upload everything at once.** Drop every statement for a month in one go: PDF, CSV, Excel, OFX/QFX or screenshots.
  - Each file is matched to its account automatically, using the bank, account type, last 4 digits and account-holder name.
  - New cards and accounts are picked up on their own. Uploading the same file twice is ignored.
- **Simple progress:** Processing → Creating report → Report ready.
- **Money flow at a glance:** Money in − Spent = **Left over**, which branches into three boxes that always add up:
  - **Liquid savings:** growth of high-yield savings plus the change in checking
  - **Invested:** transfers to investment accounts plus 401(k), yours and the employer's
  - **Card bills:** this month's card payments minus this month's charges. This is just timing when cards are paid in full.

  It also shows a **savings rate**: left over ÷ money in.
- **Breakdown** in collapsible sections:
  - **Income:** every source with a nickname (e.g. "Acme salary", "401(k) employer match"). Click to see the exact deposits.
  - **Spending:** each transaction counted exactly once, in one of these buckets: Rent + Utilities, Groceries, Fuel, Eating Out, Shopping, Vehicle, Flights, Hotels, Subscriptions, Zelle Payments and Miscellaneous. Each shows its total and % of money in, with click-to-expand transactions. Then come your **Subscriptions** (with price changes) and **6 suggestion cards** written by Claude.
  - **Liquid savings:** savings and checking balances, interest earned, estimated APY.
  - **Investments:** start and end value, contributions, and return (ROI) per account, e.g. 401(k) and brokerage.
- **Household views:** **Household** (everything) plus one view per person, showing their income, their own cards, savings, investments and savings rate.
- **Calendar months:** each transaction counts in the month it happened, whichever statement it came from.
  - Card cycles that cross months (e.g. Aug 24 – Sep 23) are split correctly.
  - A month with only spillover transactions is marked "Leftovers only".
  - A month is marked **Complete** once it and the months before and after are uploaded. Until then, a note lists the exact days still missing.
- **Light and dark mode** toggle; works on phone-size screens too.

## Setup (once)
Requires macOS, Python 3.9+, and the Claude desktop app (for reading PDFs and writing suggestions).

```bash
git clone <your-repo-url> finance-tracker && cd finance-tracker
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p data && cp household.example.json data/household.json
```

Edit `data/household.json` with your app title, household members, and the names you each appear under on statements.

### Connect Claude
The app uses the copy of Claude Code that ships with the Claude desktop app, so there's nothing extra to install. Log it in once by running this, then finish the login in your browser:
```bash
"$(ls -td "$HOME/Library/Application Support/Claude/claude-code/"*/*/claude.app/Contents/MacOS/claude | head -1)" /login
```
Without Claude:
- CSV, Excel and OFX exports still work fully.
- PDFs are read on a best-effort basis.
- Screenshots are skipped.
- Suggestions are basic.

## Run it
```bash
.venv/bin/python app/server.py
```
Open http://127.0.0.1:8765.

1. **Home:** pick the month and year (any in the last 5 years), drop in all that month's statements, click **Create report**, then **View report →**.
2. **Report:** pick a month (● = has a report, ◐ = leftovers only) and a view (Household or a person).
   - If the report asks a one-time question like "Whose income is X?", answer it once and it's remembered.

## How it works
- **Reading statements:** scripts parse CSV, Excel and OFX. Claude reads PDFs, screenshots and unusual layouts.
- **Categorizing,** in order, first match wins:
  1. Keyword rules for known merchants (`app/report.py`)
  2. Claude's remembered answer for that merchant
  3. Claude's label from reading the statement
  4. The "same price every month" subscription guess
  5. The bank's own category
  6. Miscellaneous
- **No double counting:**
  - Card payments are matched to the card statement and treated as transfers.
  - Transfers between your own accounts, and between household members, are excluded.
  - Refunds net against their bucket.
- **Statements you haven't uploaded:** card bills paid with no matching card statement, and money from a member's account that isn't uploaded, are labeled as estimates. They're replaced automatically once the statement is uploaded.
- **Nothing household-specific is hard-coded:**
  - Account owners are read from the statements.
  - Employers are learned, or asked about once.
  - Joint-account contributions are worked out from actual deposits.

## Privacy
- The server listens only on 127.0.0.1, rejects requests from other sites, and the page loads nothing from the internet.
- **Claude only sees:**
  - files the scripts can't read themselves
  - merchant names (no amounts) when sorting unfamiliar ones
  - the report's totals when writing suggestions
- **All personal data stays in `data/`,** which is git-ignored:
  - the statements you upload
  - `ledger.sqlite` (every transaction, account numbers cut to the last 4 digits)
  - `accounts.json`
  - `household.json`
  - your answers to the one-time questions
- To start over, delete the `data/` folder.

## Project layout
| Path | What |
|---|---|
| `app/server.py` | local web server and API |
| `app/pipeline.py` | processing: read files → ledger → report + suggestions |
| `app/parsers.py` | CSV / Excel / OFX / PDF readers |
| `app/accounts.py` | detects which account a file belongs to |
| `app/household.py` | household members, whose account / income is whose |
| `app/report.py` | categorizing, money flow, the monthly report |
| `app/claude.py` | calls to Claude (statement reading, merchant sorting, suggestions) |
| `app/store.py` | storage (SQLite ledger, paths) |
| `web/index.html` | the whole UI (Home + Report) |
| `household.example.json` | template for `data/household.json` |
