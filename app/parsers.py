"""Deterministic readers for the formats scripts handle reliably: CSV/TSV, Excel, OFX/QFX.

PDFs and images go to Claude (see claude.py). When Claude isn't available, PDFs get a best-effort
text read (pdf_fallback) for statement totals and simple transaction lines.
"""
import csv
import io
import re
from pathlib import Path

from store import parse_amount, parse_date

TABLE_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}
OFX_EXT = {".ofx", ".qfx", ".qbo"}
PDF_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".gif"}
ALLOWED_EXT = TABLE_EXT | OFX_EXT | PDF_EXT | IMAGE_EXT

SYN = {
    "date": ["transaction date", "trans date", "trans. date", "date", "posting date", "post date", "posted date",
             "run date", "activity date", "datetime"],
    "description": ["description", "merchant", "payee", "name", "details", "memo", "transaction", "action", "narrative", "note"],
    "amount": ["amount", "amount ($)", "amount (usd)", "amount usd", "transaction amount", "net amount", "amount (total)"],
    "debit": ["debit", "debits", "withdrawal", "withdrawals", "money out", "charges", "charge"],
    "credit": ["credit", "credits", "deposit", "deposits", "money in", "payments"],
    "balance": ["balance", "running balance", "current balance", "running bal.", "account balance", "available balance"],
    "category": ["category"],
    "status": ["status"],
}


class Unreadable(Exception):
    pass


def read_table(path):
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        return [["" if c is None else c for c in row] for row in wb.active.iter_rows(values_only=True)]
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    try:
        delim = csv.Sniffer().sniff(raw[:4096], delimiters=",\t;|").delimiter
    except csv.Error:
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
    return list(csv.reader(io.StringIO(raw), delimiter=delim))


def find_header(rows):
    for i, row in enumerate(rows[:30]):
        cells = [str(c).strip().lower() for c in row]
        if any(c in SYN["date"] for c in cells) and any(c in SYN["amount"] + SYN["debit"] + SYN["credit"] for c in cells):
            return i
    return None


def table_headers(path):
    rows = read_table(path)
    hi = find_header(rows)
    if hi is None:
        return [], rows[:15], []
    return [str(h).strip().lower() for h in rows[hi]], rows[:hi], rows[hi + 1:hi + 60]


def parse_table(path, account_type):
    """Return (rows, summaries). rows: [{date, description, amount, bank_category}], amount negative = money out."""
    rows = read_table(path)
    hi = find_header(rows)
    if hi is None:
        raise Unreadable("no header row with date + amount columns")
    headers = [str(h).strip().lower() for h in rows[hi]]
    idx = {}
    for key, names in SYN.items():
        for n in names:
            if n in headers:
                idx[key] = headers.index(n)
                break
    if "date" not in idx or "description" not in idx or not ({"amount", "debit", "credit"} & set(idx)):
        raise Unreadable(f"unrecognized columns: {headers}")
    out, balances = [], []
    for row in rows[hi + 1:]:
        if not any(str(c).strip() for c in row):
            continue
        def get(k):
            return row[idx[k]] if k in idx and idx[k] < len(row) else None
        if str(get("status") or "").strip().lower() == "pending":
            continue
        try:
            d = parse_date(get("date"))
        except (ValueError, TypeError):
            continue
        if "amount" in idx:
            amt = parse_amount(get("amount"))
        else:
            amt = (parse_amount(get("credit")) or 0) - abs(parse_amount(get("debit")) or 0)
        if amt is None:
            continue
        out.append({"date": d, "description": " ".join(str(get("description") or "").split()), "amount": amt,
                    "bank_category": str(get("category") or "").strip() or None})
        b = parse_amount(get("balance")) if "balance" in idx else None
        if b is not None:
            balances.append((d, b, amt))
    if not out:
        raise Unreadable("no transactions found")
    # Card exports disagree on sign (Chase: charges negative; Amex/Capital One: positive). Normalize.
    if "amount" in idx and account_type == "credit_card":
        purchases = [r for r in out if not re.search(r"payment|thank you|credit|refund", r["description"], re.I)]
        if sum(1 for r in purchases if r["amount"] > 0) > len(purchases) / 2:
            for r in out:
                r["amount"] = -r["amount"]
    # Running balances -> start/end balance for each month the file covers.
    summaries = []
    if balances:
        if balances[0][0] > balances[-1][0]:
            balances.reverse()  # newest-first export: walk oldest-first
        by_month = {}
        for d, b, amt in balances:
            by_month.setdefault(d[:7], []).append((d, b, amt))
        for m, items in sorted(by_month.items()):
            first_d, first_b, first_amt = items[0]
            summaries.append({"period_start": first_d, "period_end": items[-1][0], "approx": 1,
                              "start_balance": round(first_b - first_amt, 2), "end_balance": items[-1][1]})
    return out, summaries


def parse_ofx(path, account_type):
    raw = Path(path).read_text(errors="replace")
    out = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", raw, re.S | re.I):
        def tag(name):
            m = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
            return m.group(1).strip() if m else ""
        d, amt = tag("DTPOSTED")[:8], parse_amount(tag("TRNAMT"))
        if d and amt is not None:
            out.append({"date": parse_date(d, fmt="%Y%m%d"), "description": " ".join(filter(None, [tag("NAME"), tag("MEMO")])),
                        "amount": amt, "bank_category": None})
    summaries = []
    m = re.search(r"<LEDGERBAL>.*?<BALAMT>([^<\r\n]+).*?<DTASOF>(\d{8})", raw, re.S | re.I)
    if m:
        b = parse_amount(m.group(1))
        summaries.append({"end_balance": -b if account_type == "credit_card" else b, "period_end": parse_date(m.group(2), fmt="%Y%m%d")})
    if not out and not summaries:
        raise Unreadable("OFX had no transactions")
    return out, summaries


def pdf_text(path, max_pages=None):
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
        return "\n".join((p.extract_text() or "") for p in pages)


def _money_after(label, text):
    m = re.search(label + r"[^\n$\d-]{0,40}\$?\s*(?P<amt>-?[\d,]+\.\d{2})", text, re.I)
    return parse_amount(m.group("amt")) if m else None


_DATE_WORDS = r"(?:[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{2,4})"


def statement_end(text):
    """End date of a statement period like 'September 1, 2026 - September 30, 2026' or '08/15/26 - 09/14/26'."""
    m = re.search(rf"{_DATE_WORDS}\s*(?:-|–|to|through)\s*({_DATE_WORDS})", text)
    if m:
        try:
            return parse_date(m.group(1))
        except ValueError:
            return None
    return None


def pdf_fallback(path, account_type):
    """Best effort without Claude: statement totals + 'MM/DD description amount' lines."""
    text = pdf_text(path)
    if len(text.strip()) < 50:
        raise Unreadable("scanned PDF (no text) - needs Claude")
    summary = {
        "start_balance": _money_after(r"(beginning|opening|previous|starting)\s+(account\s+|portfolio\s+)?(balance|value)", text),
        "end_balance": _money_after(r"(ending|closing|new)\s+(account\s+|portfolio\s+)?(balance|value)", text),
        "contributions": _money_after(r"(additions|contributions|deposits)", text) if account_type == "investment" else None,
        "interest": _money_after(r"interest (earned|paid)", text),
        "dividends": _money_after(r"dividends", text) if account_type == "investment" else None,
    }
    ym = re.search(r"(20\d\d)", text)
    year = ym.group(1) if ym else None
    rows = []
    for line in text.splitlines():
        m = re.match(r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(.+?)\s+(-?\$?[\d,]+\.\d{2})$", line.strip())
        if not m:
            continue
        ds = m.group(1) if m.group(1).count("/") == 2 or not year else f"{m.group(1)}/{year}"
        try:
            d = parse_date(ds)
        except ValueError:
            continue
        amt = parse_amount(m.group(3))
        if account_type == "credit_card" and not re.search(r"payment|credit|refund", m.group(2), re.I):
            amt = -abs(amt)
        rows.append({"date": d, "description": " ".join(m.group(2).split()), "amount": amt, "bank_category": None})
    # Bank PDFs often print deposits and withdrawals both unsigned; without signs we'd misread
    # spending as income, so only trust lines when the statement shows explicit negatives.
    if account_type not in ("credit_card", "investment") and not any(r["amount"] < 0 for r in rows):
        rows = []
    summary = {k: v for k, v in summary.items() if v is not None}
    if not rows and not summary:
        raise Unreadable("couldn't find transactions in this PDF - needs Claude")
    if summary:
        summary["period_end"] = statement_end(text) or max((r["date"] for r in rows), default=None)
    return rows, [summary] if summary and summary.get("period_end") else []
