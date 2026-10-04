"""Paths, the transaction ledger, and small helpers. Everything lives under data/."""
import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

ROOT = Path(os.environ.get("FINANCE_HOME") or Path(__file__).resolve().parent.parent)
DATA = ROOT / "data"
DB_PATH = DATA / "ledger.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  month TEXT NOT NULL,
  amount REAL NOT NULL,          -- negative = money out of the account, positive = money in
  description TEXT NOT NULL,
  bank_category TEXT,            -- category column from the bank export, if any
  bucket_hint TEXT,              -- spending bucket suggested by Claude when it extracted the file
  account_id TEXT NOT NULL,
  file_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_month ON transactions(month);
CREATE TABLE IF NOT EXISTS files (
  hash TEXT PRIMARY KEY,
  month TEXT NOT NULL,           -- month it was uploaded for
  name TEXT NOT NULL,
  account_id TEXT,
  status TEXT NOT NULL,          -- read | unreadable
  method TEXT,                   -- script | claude
  rows INTEGER DEFAULT 0,
  note TEXT
);
CREATE TABLE IF NOT EXISTS summaries (
  file_hash TEXT NOT NULL,       -- statement-level numbers (balances, contributions, interest), one row per month covered
  account_id TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  start_balance REAL,
  end_balance REAL,
  contributions REAL,
  withdrawals REAL,
  interest REAL,
  dividends REAL,
  label TEXT,                    -- e.g. "401(k)", "Individual", "High-Yield Savings"
  approx INTEGER DEFAULT 0,      -- 1 = period inferred from first/last transaction (CSV exports), not printed on a statement
  PRIMARY KEY (file_hash, period_end)
);
"""


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(summaries)")}
    if "label" not in cols:  # databases created before labels existed
        conn.execute("ALTER TABLE summaries ADD COLUMN label TEXT")
    if "approx" not in cols:
        conn.execute("ALTER TABLE summaries ADD COLUMN approx INTEGER DEFAULT 0")
    return conn


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    tmp.replace(path)


def month_dir(month):
    return DATA / month


def uploads_dir(month):
    return DATA / month / "uploads"


def file_hash(data):
    return hashlib.sha256(data).hexdigest()


def tx_id(account_id, d, amount, description, occurrence):
    raw = f"{account_id}|{d}|{amount:.2f}|{description.strip().lower()}|{occurrence}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def mask_numbers(text):
    """Keep only the last 4 digits of long digit runs (account/card numbers)."""
    return re.sub(r"\d[\d\- ]{7,}\d", lambda m: "••" + re.sub(r"\D", "", m.group())[-4:], text)


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y",
                 "%Y/%m/%d", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d"]


def parse_date(s, fmt=None):
    if isinstance(s, (date, datetime)):
        return s.strftime("%Y-%m-%d")
    s = str(s or "").strip()
    if fmt:
        return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
    s = s.split(" (")[0].strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"unrecognized date: {s!r}")


def parse_amount(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or "-" in s[:3] or s.endswith("-")
    digits = re.sub(r"[^\d.]", "", s)
    if not digits or digits == ".":
        return None
    v = float(digits)
    return -v if neg else v


def month_bounds(month):
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start.isoformat(), end.isoformat()  # [start, end)


def months_back(n):
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out
