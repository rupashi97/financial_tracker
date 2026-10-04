"""Processing for one month: read every uploaded file -> ledger -> report + suggestions.

Stages (shown in the UI): processing -> creating_report -> ready
"""
import traceback
from pathlib import Path

import accounts
import claude
import household
import parsers
import report
from store import DATA, db, file_hash, load_json, mask_numbers, parse_date, save_json, tx_id, uploads_dir

# Shown on the home page. Keyed by month.
JOBS = {}


def _set(month, stage, **extra):
    JOBS[month] = {"month": month, "stage": stage, **extra}


def read_file(path):
    """Return (account, rows, summaries, method). Scripts first; Claude for anything they can't read."""
    info = accounts.detect(path)
    ext = path.suffix.lower()
    script_ok = ext in parsers.TABLE_EXT | parsers.OFX_EXT
    if script_ok and info["type"]:
        acct = accounts.resolve(info)
        if acct:
            try:
                if ext in parsers.OFX_EXT:
                    rows, sums = parsers.parse_ofx(path, acct["type"])
                else:
                    rows, sums = parsers.parse_table(path, acct["type"])
                return acct, rows, sums, "script"
            except parsers.Unreadable:
                pass
    if claude.available():
        data = claude.extract_statement(path)
        cinfo = {"institution": data.get("institution") or info["institution"],
                 "type": data.get("account_type") or info["type"], "last4": data.get("last4") or info["last4"]}
        cinfo["institution"] = accounts.institution_of(cinfo["institution"] or "") or cinfo["institution"]
        acct = accounts.resolve(cinfo, household.owner_from_holders(data.get("account_holders")))
        if not acct:
            raise parsers.Unreadable("couldn't tell which account this is")
        rows = []
        for t in data.get("transactions") or []:
            try:
                rows.append({"date": parse_date(t["date"]), "description": str(t["description"]), "amount": float(t["amount"]),
                             "bank_category": None, "bucket_hint": t.get("bucket")})
            except (KeyError, TypeError, ValueError):
                continue
        s = {k: data.get(k) for k in ("period_start", "period_end", "start_balance", "end_balance", "contributions",
                                       "withdrawals", "interest", "dividends")}
        s["label"] = data.get("account_label")
        sums = [s] if s.get("period_end") else []
        return acct, rows, sums, "claude"
    # No Claude: best effort for PDFs, otherwise give up on this file.
    acct = accounts.resolve(info) if info["type"] or info["institution"] else None
    if ext in parsers.PDF_EXT and acct:
        rows, sums = parsers.pdf_fallback(path, acct["type"])
        return acct, rows, sums, "script"
    raise parsers.Unreadable("needs Claude (not connected)" if not acct else "format not readable without Claude")


def store(conn, h, acct, rows, sums):
    conn.execute("DELETE FROM transactions WHERE file_hash = ?", (h,))
    conn.execute("DELETE FROM summaries WHERE file_hash = ?", (h,))
    # Transactions already stored from *other* statements of this account (neighbouring statements can share a
    # boundary day, and the same charge may be worded slightly differently on each).
    existing = {}
    for e in conn.execute("SELECT date, amount FROM transactions WHERE account_id = ? AND file_hash != ?", (acct["id"], h)):
        k = (e["date"], round(e["amount"], 2))
        existing[k] = existing.get(k, 0) + 1
    seen, used = {}, {}
    for r in rows:
        desc = mask_numbers(r["description"])
        amt = round(float(r["amount"]), 2)
        dk = (r["date"], amt)
        if used.get(dk, 0) < existing.get(dk, 0):  # already counted from another statement
            used[dk] = used.get(dk, 0) + 1
            continue
        key = (r["date"], amt, desc.lower())
        seen[key] = seen.get(key, 0) + 1
        # Same account + date + amount + description = same transaction, even if it shows up in two uploads.
        conn.execute("INSERT OR IGNORE INTO transactions (id, date, month, amount, description, bank_category, bucket_hint, account_id, file_hash) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (tx_id(acct["id"], r["date"], amt, desc, seen[key]), r["date"], r["date"][:7], amt, desc,
                      r.get("bank_category"), r.get("bucket_hint"), acct["id"], h))
    for s in sums:
        if not s.get("period_end"):
            continue
        conn.execute("INSERT OR REPLACE INTO summaries (file_hash, account_id, period_start, period_end, start_balance, end_balance, "
                     "contributions, withdrawals, interest, dividends, label, approx) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (h, acct["id"], s.get("period_start"), parse_date(s["period_end"]), s.get("start_balance"), s.get("end_balance"),
                      s.get("contributions"), s.get("withdrawals"), s.get("interest"), s.get("dividends"), s.get("label"),
                      1 if s.get("approx") else 0))


def sort_merchants(month):
    """Ask Claude about merchants our rules couldn't place (names only); remembered for next time."""
    if not claude.available():
        return
    unknown = report.unknown_merchants(month)
    if not unknown:
        return
    try:
        answers = claude.bucket_merchants(sorted(set(unknown.values())))
        cache = load_json(report.MERCHANT_CACHE, {})
        for m, desc in unknown.items():
            if desc in answers:
                cache[m] = answers[desc]
        save_json(report.MERCHANT_CACHE, cache)
    except Exception:
        pass  # buckets fall back to Miscellaneous


def write_suggestions(month):
    rep = report.build(month)
    if not rep["has_data"]:
        return rep
    items, source = None, "rules"
    if claude.available():
        try:
            items, source = claude.write_suggestions(report.facts_for_suggestions(rep)), "claude"
        except Exception:
            items = None
    if not items:
        items, source = report.rule_suggestions(rep), "rules"
    save_json(DATA / month / "suggestions.json", {"items": items, "source": source})
    return rep


def process(month):
    try:
        _set(month, "processing")
        if not claude.available():  # e.g. a usage limit that has since reset, or a fresh login
            claude.check()
        conn = db()
        done = {r["hash"] for r in conn.execute("SELECT hash FROM files WHERE status = 'read'")}
        new_hashes = []
        for path in sorted(uploads_dir(month).glob("*")):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in parsers.ALLOWED_EXT:
                continue
            h = file_hash(path.read_bytes())
            if h in done:
                continue
            try:
                acct, rows, sums, method = read_file(path)
                store(conn, h, acct, rows, sums)
                conn.execute("INSERT OR REPLACE INTO files (hash, month, name, account_id, status, method, rows, note) VALUES (?,?,?,?,?,?,?,?)",
                             (h, month, path.name, acct["id"], "read", method, len(rows), None))
                new_hashes.append(h)
            except Exception as e:  # one bad file shouldn't stop the month
                conn.execute("INSERT OR REPLACE INTO files (hash, month, name, account_id, status, method, rows, note) VALUES (?,?,?,?,?,?,?,?)",
                             (h, month, path.name, None, "unreadable", None, 0, str(e)[:300]))
            conn.commit()

        _set(month, "creating_report")
        # Neighbouring months that just gained transactions (e.g. Aug 24-31 on September's statements)
        # and already have their own report: refresh those too, so their suggestions match the new totals.
        touched = set()
        if new_hashes:
            marks = ",".join("?" * len(new_hashes))
            touched = {r[0] for r in conn.execute(f"SELECT DISTINCT month FROM transactions WHERE file_hash IN ({marks})", new_hashes)}
        uploaded = {r[0] for r in conn.execute("SELECT DISTINCT month FROM files WHERE status = 'read'")}
        neighbours = sorted(m for m in touched if m != month and m in uploaded)
        for m in [month] + neighbours:
            sort_merchants(m)
        rep = write_suggestions(month)
        for m in neighbours:
            write_suggestions(m)
        _set(month, "ready", has_data=rep["has_data"])
    except Exception as e:
        traceback.print_exc()
        _set(month, "error", message=str(e)[:200])
