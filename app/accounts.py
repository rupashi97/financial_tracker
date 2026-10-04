"""Work out which account a statement belongs to, and keep a small registry of accounts.

Account types: checking | savings | investment | credit_card | other
"""
import re
from pathlib import Path

import parsers
from store import DATA, load_json, save_json

REGISTRY = DATA / "accounts.json"
DEFAULT_ACCOUNTS = []  # accounts are created automatically from the statements you upload

INSTITUTIONS = [
    ("Chase", r"\bchase\b|jpmorgan"), ("SoFi", r"\bsofi\b"), ("Fidelity", r"fidelity"),
    ("American Express", r"american express|\bamex\b"), ("Capital One", r"capital ?one"),
    ("Citi", r"\bciti(bank|cards?)?\b"), ("Discover", r"\bdiscover\b"), ("Bank of America", r"bank of america|\bbofa\b"),
    ("Wells Fargo", r"wells ?fargo"), ("Apple Card", r"apple ?card|goldman sachs"), ("Venmo", r"venmo"),
    ("PayPal", r"paypal"), ("Schwab", r"schwab"), ("Vanguard", r"vanguard"), ("Robinhood", r"robinhood"),
    ("Ally", r"\bally\b"), ("Marcus", r"\bmarcus\b"), ("Barclays", r"barclays"), ("Bilt", r"\bbilt\b"),
]

# Known export layouts: (required lowercase headers, institution, type)
HEADER_SIGNATURES = [
    ({"details", "posting date", "description", "amount", "check or slip #"}, "Chase", "checking"),
    ({"transaction date", "post date", "description", "category", "type", "amount", "memo"}, "Chase", "credit_card"),
    ({"transaction date", "posted date", "card no.", "description", "debit", "credit"}, "Capital One", "credit_card"),
    ({"trans. date", "post date", "description", "amount", "category"}, "Discover", "credit_card"),
    ({"transaction date", "clearing date", "description", "merchant", "amount (usd)"}, "Apple Card", "credit_card"),
    ({"date", "description", "amount", "extended details"}, "American Express", "credit_card"),
    ({"date", "description", "card member", "account #", "amount"}, "American Express", "credit_card"),
    ({"date", "description", "type", "amount", "current balance", "status"}, "SoFi", None),
    ({"run date", "action", "symbol"}, "Fidelity", "investment"),
    ({"datetime", "type", "from", "to", "amount (total)"}, "Venmo", "other"),
]

TYPE_KEYWORDS = {
    "credit_card": [r"minimum payment due", r"payment due date", r"credit (limit|line)", r"available credit", r"new balance",
                    r"card ?member", r"interest charged", r"purchase apr"],
    "checking": [r"\bchecking\b", r"checks? paid", r"deposits and additions", r"electronic withdrawals", r"daily ending balance"],
    "savings": [r"\bsavings\b", r"high[- ]yield", r"annual percentage yield", r"\bapy\b", r"interest earned", r"\bvaults?\b"],
    "investment": [r"investment report", r"brokerage", r"holdings", r"\bpositions\b", r"account value", r"portfolio",
                   r"\bira\b", r"401\(?k\)?", r"\broth\b", r"\bshares\b"],
}
TYPE_LABEL = {"checking": "Checking", "savings": "Savings", "investment": "Investments", "credit_card": "Card", "other": ""}


def institution_of(text):
    for name, rx in INSTITUTIONS:
        if text and re.search(rx, text, re.I):
            return name
    return None


def find_last4(text):
    for p in (r"(?:ending in|ending|card ending|acct ending)\s*[:#]?\s*[x•*.\-\s]*(\d{4})\b",
              r"(?:account|acct|card)\s*(?:number|no\.?|#)\s*:?\s*[A-Za-z0-9xX•*\-\s]{0,24}?(\d{4})\b",
              r"[xX*•]{3,}[\s\-]*(\d{4})\b"):
        m = re.search(p, text or "", re.I)
        if m:
            return m.group(1)
    return None


def detect(path):
    """Script-only detection: {institution, type, last4}. Any field may be None."""
    path = Path(path)
    ext = path.suffix.lower()
    inst = atype = last4 = None
    head = ""
    if ext in parsers.OFX_EXT:
        raw = path.read_text(errors="replace")
        if re.search(r"<CREDITCARDMSGSRSV1>|<CCACCTFROM>", raw, re.I):
            atype = "credit_card"
        elif re.search(r"<INVSTMTMSGSRSV1>", raw, re.I):
            atype = "investment"
        else:
            m = re.search(r"<ACCTTYPE>(\w+)", raw, re.I)
            atype = {"CHECKING": "checking", "SAVINGS": "savings", "MONEYMRKT": "savings"}.get(m.group(1).upper()) if m else None
        m = re.search(r"<ORG>([^<\r\n]+)", raw, re.I)
        inst = institution_of(m.group(1)) if m else None
        m = re.search(r"<ACCTID>([^<\r\n]+)", raw, re.I)
        last4 = re.sub(r"\D", "", m.group(1))[-4:] if m and len(re.sub(r"\D", "", m.group(1))) >= 4 else None
    elif ext in parsers.TABLE_EXT:
        headers, preamble, body = parsers.table_headers(path)
        hs = set(headers)
        for req, s_inst, s_type in HEADER_SIGNATURES:
            if req <= hs:
                inst, atype = s_inst, s_type
                break
        head = " ".join(" ".join(str(c) for c in r) for r in preamble)
        for col in ("card no.", "card number", "account number", "account #"):
            if col in headers:
                vals = [re.sub(r"\D", "", str(r[headers.index(col)])) for r in body if len(r) > headers.index(col)]
                vals = [v for v in vals if len(v) >= 4]
                if vals:
                    last4 = vals[0][-4:]
                break
        if inst == "SoFi" and not atype:
            text = " ".join(" ".join(str(c) for c in r) for r in body)
            atype = "savings" if re.search(r"interest", text, re.I) and not re.search(r"debit card|pos ", text, re.I) else None
    elif ext in parsers.PDF_EXT:
        try:
            head = parsers.pdf_text(path, max_pages=2)
        except Exception:
            head = ""
    # Institution from the statement header first (transaction lines mention other banks), then the filename.
    inst = inst or institution_of(head[:1500]) or institution_of(path.stem.replace("_", " ").replace("-", " "))
    if not atype and head:
        scores = {t: sum(1 for p in pats if re.search(p, head, re.I)) for t, pats in TYPE_KEYWORDS.items()}
        best = max(scores.values())
        tops = [t for t, s in scores.items() if s == best]
        if best >= 2 and len(tops) == 1:
            atype = tops[0]
    if not atype:
        fl = path.stem.lower()
        for t, rx in (("savings", r"saving|hysa|vault"), ("checking", r"checking"), ("credit_card", r"card|credit"),
                      ("investment", r"invest|brokerage|ira|401k")):
            if re.search(rx, fl):
                atype = t
                break
    if not atype and inst in ("Venmo", "PayPal"):
        atype = "other"
    if not atype and inst in ("American Express", "Discover", "Apple Card", "Capital One", "Barclays", "Bilt"):
        atype = "credit_card"
    last4 = last4 or find_last4(head[:3000])
    if not last4:
        m = re.match(r"^[A-Za-z]+[ _-]?(\d{4})[_ -]", path.name)  # e.g. Chase7781_Activity_2026.CSV
        last4 = m.group(1) if m else None
    return {"institution": inst, "type": atype, "last4": last4}


def load():
    reg = load_json(REGISTRY)
    if reg is None:
        reg = {"accounts": [dict(a) for a in DEFAULT_ACCOUNTS]}
        save_json(REGISTRY, reg)
    return reg["accounts"]


def by_id():
    return {a["id"]: a for a in load()}


def resolve(info, owner=None):
    """Find or create the account for detected {institution, type, last4}. Returns account dict or None.
    owner = member id or "joint", from the names printed on the statement (None if unknown, e.g. CSV exports).
    Accounts that belong to someone else are never matched, so two people's Chase cards stay separate."""
    accounts = load()
    inst, atype, last4 = info.get("institution"), info.get("type"), info.get("last4")
    fits = [a for a in accounts if not owner or not a.get("owner") or a["owner"] == owner]

    def claim(acct):
        changed = False
        if last4 and not acct.get("last4"):
            acct["last4"], changed = last4, True
        if owner and not acct.get("owner"):
            acct["owner"], changed = owner, True
        if changed:
            save_json(REGISTRY, {"accounts": accounts})
        return acct

    if last4:
        hit = [a for a in accounts if a.get("last4") == last4 and (not atype or a["type"] == atype)]
        if hit:
            return claim(hit[0])
    if not atype:
        same_inst = [a for a in fits if inst and a["institution"] == inst]
        return claim(same_inst[0]) if len(same_inst) == 1 else None
    if not inst:
        same_type = [a for a in fits if a["type"] == atype]
        return claim(same_type[0]) if len(same_type) == 1 and atype != "credit_card" else None
    same = [a for a in fits if a["institution"] == inst and a["type"] == atype]
    candidates = [a for a in same if not a.get("last4")] if last4 else same
    if len(candidates) == 1 or (candidates and not last4):
        return claim(candidates[0])
    # New account.
    import household
    who = {"joint": "Joint"}.get(owner) or household.member_names().get(owner)
    label = TYPE_LABEL[atype]
    slug = re.sub(r"[^a-z0-9]+", "-", f"{owner or ''} {inst} {label} {last4 or ''}".lower()).strip("-")
    ids = {a["id"] for a in accounts}
    base, n = slug, 2
    while slug in ids:
        slug, n = f"{base}-{n}", n + 1
    name = f"{inst} {label}".strip() + (f" ••{last4}" if last4 else "")
    acct = {"id": slug, "name": f"{who}'s {name}" if who and who != "Joint" else (f"Joint {name}" if who else name),
            "type": atype, "institution": inst}
    if last4:
        acct["last4"] = last4
    if owner:
        acct["owner"] = owner
    accounts.append(acct)
    save_json(REGISTRY, {"accounts": accounts})
    return acct
