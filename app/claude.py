"""Calls the Claude Code CLI in headless mode (`claude -p`).

Used for three things only:
  1. reading statements scripts can't (PDFs, screenshots, odd layouts) - Claude gets read access to that one file
  2. sorting unfamiliar merchant names into spending buckets - names only, no amounts
  3. writing the monthly suggestions - from the report's totals, not raw statements
If the CLI isn't installed or logged in, everything degrades gracefully (see available()).
"""
import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

BUCKETS = ["Rent + Utilities", "Groceries", "Fuel", "Eating Out", "Shopping", "Vehicle", "Flights", "Hotels", "Subscriptions", "Zelle Payments",
           "Miscellaneous"]
CANDIDATES = ["~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
# The Claude desktop app bundles Claude Code here; the version folder changes when the app updates.
DESKTOP_BUNDLE = "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"

_status = {"ok": None, "reason": None}  # filled by check()


def cli_path():
    p = shutil.which("claude")
    if p:
        return p
    for c in CANDIDATES:
        c = os.path.expanduser(c)
        if os.path.exists(c):
            return c
    bundled = glob.glob(os.path.expanduser(DESKTOP_BUNDLE))
    return max(bundled, key=os.path.getmtime) if bundled else None


def check():
    """Is Claude usable (installed AND logged in)? Cheap: a logged-out CLI answers instantly without an API call."""
    if not cli_path():
        _status.update(ok=False, reason="not_installed")
        return _status
    try:
        _run("Reply with just: OK", timeout=60)
        _status.update(ok=True, reason=None)
    except Exception as e:
        _status.update(ok=False, reason="not_logged_in" if "log" in str(e).lower() else "error")
    return _status


def available():
    if _status["ok"] is None:
        check()
    return bool(_status["ok"])


def status():
    if _status["ok"] is None:
        check()
    return dict(_status, path=cli_path())


def _run(prompt, cwd=None, allow_read=False, timeout=420):
    cli = cli_path()
    if not cli:
        raise RuntimeError("Claude CLI not installed")
    cmd = [cli, "-p", prompt, "--output-format", "json"]
    if allow_read:
        cmd += ["--allowedTools", "Read"]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError((proc.stderr or proc.stdout or "claude failed").strip()[:300])
    if out.get("is_error"):
        if "log" in str(out.get("result", "")).lower():
            _status.update(ok=False, reason="not_logged_in")
        raise RuntimeError(str(out.get("result"))[:300])
    return out.get("result", "")


def _json_from(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


def extract_statement(path):
    path = Path(path)
    prompt = f"""Read the financial statement file "{path.name}" in the current directory and extract its data.
Reply with ONLY one JSON object, no prose, in exactly this shape:
{{"institution": "bank/issuer name or null",
 "account_type": "checking | savings | investment | credit_card | other",
 "last4": "last 4 digits of the account/card number, or null",
 "account_holders": ["names of the account holder(s) exactly as printed"],
 "account_label": "short name of the specific account, e.g. 401(k), Individual brokerage, Roth IRA, Savings, or null",
 "period_start": "YYYY-MM-DD or null", "period_end": "YYYY-MM-DD or null",
 "start_balance": number or null, "end_balance": number or null,
 "contributions": number or null, "withdrawals": number or null, "interest": number or null, "dividends": number or null,
 "transactions": [{{"date": "YYYY-MM-DD", "description": "text as printed", "amount": number, "bucket": "..."}}]}}
Rules:
- amount sign: NEGATIVE = money leaving this account, POSITIVE = money coming in. For credit cards: purchases/fees negative, payments/refunds positive.
- Include every posted transaction exactly once. Skip pending items, subtotals, and running-balance lines.
- For investment statements: start/end_balance = beginning/ending account value; contributions = deposits/transfers in; withdrawals = money taken out; list only cash movements (contributions, withdrawals, dividends) as transactions, not individual buy/sell trades.
- For credit cards, end_balance = new balance owed (positive).
- interest = interest earned this period (savings/checking).
- bucket: for money going out, one of {BUCKETS}; otherwise null.
- Never output full account numbers; last 4 digits at most.
- Use the statement's own year for dates."""
    return _json_from(_run(prompt, cwd=str(path.parent), allow_read=True))


def bucket_merchants(descriptions):
    if not descriptions:
        return {}
    listing = "\n".join(f"- {d}" for d in descriptions)
    prompt = f"""Sort each US bank/card transaction description into exactly one spending bucket:
{BUCKETS}
Guidance: Rent + Utilities = rent, mortgage, electric, gas utility, water, internet, phone. Fuel = gas stations/EV charging.
Eating Out = restaurants, cafes, bars, food delivery. Vehicle = car insurance, DMV/registration, parking, tolls, rental cars,
car wash/detailing, car maintenance and repairs, tires, auto parts (gas itself is Fuel). Flights = airline tickets and airline fees. Hotels = hotels, motels, resorts, Airbnb/VRBO.
Groceries includes Indian/Asian/ethnic grocery stores and bazaars (e.g. Patel Brothers, H Mart, 99 Ranch).
Subscriptions = recurring services/memberships (streaming, software, gym, news, cloud storage) - NOT stores you happen to visit monthly.
Shopping = retail/online goods. Zelle Payments = Zelle transfers to other people.
Anything else (rideshare, medical, personal transfers, fees, etc.) = Miscellaneous.
Reply with ONLY a JSON object mapping each description exactly as given to its bucket.
{listing}"""
    out = _json_from(_run(prompt, timeout=240))
    return {k: v for k, v in out.items() if v in BUCKETS}


SUGGESTION_TYPES = ("cut", "watch", "good", "action")


def write_suggestions(facts):
    """Exactly 6 crisp suggestion cards, most important first."""
    prompt = f"""You help a couple understand their month and plan better. Their numbers (USD) as JSON:
{json.dumps(facts, indent=1)}
Give the 6 most useful suggestions for next month, most important first. Be crisp - these show as small cards.
Reply with ONLY this JSON, no prose:
{{"items": [{{"title": "max 5 words", "detail": "ONE sentence, max 18 words, citing a real number from the data",
  "impact": "short like '≈ $120/mo saved' or '+$500 to savings', or null", "type": "cut | watch | good | action"}}]}}
type: cut = spend less on something; watch = keep an eye on it; good = something going well; action = a concrete to-do
(e.g. upload a missing statement, cancel a subscription). Include at least one "good". No investment product advice. Don't moralize."""
    out = _json_from(_run(prompt, timeout=240))
    items = []
    for it in out.get("items", [])[:6]:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        items.append({"title": str(it["title"])[:60], "detail": str(it.get("detail") or "")[:220],
                      "impact": (str(it["impact"])[:40] if it.get("impact") else None),
                      "type": it.get("type") if it.get("type") in SUGGESTION_TYPES else "watch"})
    if not items:
        raise RuntimeError("no suggestions returned")
    return items
