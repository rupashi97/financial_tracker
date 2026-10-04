"""Build the monthly report from the ledger.

Every transaction lands in exactly one place:
  transfer between your own accounts / card payment  -> ignored (would double count)
  money to savings or investments                     -> Liquid Savings / Investments sections
  money in (not a transfer or refund)                 -> Income
  money out                                           -> exactly one spending bucket
"""
import re
from collections import defaultdict
from datetime import date, timedelta

import accounts as accts_mod
import household
from claude import BUCKETS
from store import DATA, db, load_json, month_bounds

MERCHANT_CACHE = DATA / "merchant_buckets.json"   # Claude's answers for unfamiliar merchants, reused next time

CARD_PAYMENT = re.compile(
    r"payment thank you|autopay|pymt|e-?payment|online payment|payment received|mobile payment|automatic payment|"
    r"credit crd|card ?payment|cardmember serv|applecard gsbank|amex epayment|american express ach|capital one.*(pmt|payment)|"
    r"discover.*payment|citi.*(autopay|payment)|bilt.*payment|payment to .{0,30}card|card ending", re.I)
OWN_TRANSFER = re.compile(
    r"online transfer|transfer (to|from)|xfer|internal transfer|from (chase|sofi)|to (chase|sofi)|sofi bank|"
    r"\bvault\b|zelle (to|from) (self|me)|account transfer", re.I)
TO_INVEST = re.compile(r"fid bkg|fidelity|moneyline|schwab|vanguard|robinhood|wealthfront|betterment|brokerage", re.I)
TO_SAVINGS = re.compile(r"sofi|transfer to sav|to savings|high yield|hysa|marcus|ally bank", re.I)
TRANSFER_LIKE = re.compile(r"zelle|venmo|transfer|xfer|payment (to|from)|cash ?app|paypal|deposit from", re.I)
CARD_LAST4 = re.compile(r"(?:ending in|ending|card)\s*[:#]?\s*(?:x+|•+|\*+)?\s*(\d{4})\b", re.I)
REFUND = re.compile(r"refund|return|reversal|credit adj|price adjustment", re.I)
INTEREST = re.compile(r"interest|\bapy\b", re.I)
RETIREMENT = re.compile(r"employee contrib|employer contrib|employer match|company match|safe harbor|payroll contrib|"
                        r"salary deferral|elective deferral|401\(?k\)? contrib|roth contrib", re.I)
EMPLOYER = re.compile(r"employer|company|match|safe harbor", re.I)

RULES = [
    ("Zelle Payments", r"zelle"),  # first: payee names can look like store names
    ("Vehicle", r"geico|progressive|state farm|allstate|farmers ins|nationwide|liberty mutual|usaa|esurance|root ins|"
                r"auto ?insurance|car insurance|\bdmv\b|motor vehicle|vehicle reg|registration renewal|smog|"
                r"parking|parkmobile|spothero|paybyphone|\btolls?\b|fastrak|e-?z ?pass|sunpass|"
                r"hertz|\bavis\b|\benterprise\b|ent rent|national car|budget rent|alamo|\bsixt\b|\bturo\b|zipcar|getaround|rental car|"
                r"car ?wash|detailing|auto spa|jiffy lube|valvoline|firestone|pep boys|midas|meineke|discount tire|"
                r"les schwab|big o tires|goodyear|autozone|o'?reilly auto|advance auto|napa auto|auto repair|"
                r"oil change|\btires?\b|body shop|collision|tesla service|dealer service"),
    ("Flights", r"airlines?|airways|air lines|delta air|united air|united \d{6,}|american air|aa\.com|southwest air|"
                r"southwes \d|jetblue|alaska air|spirit air|frontier air|flyfrontier|hawaiian air|sun country|allegiant|"
                r"air canada|lufthansa|british a|emirates|qatar air|air india|singapore air|cathay|klm|air france|"
                r"vistara|indigo air|etihad|turkish air|virgin atlantic|aer lingus|westjet|volaris|aeromexico"),
    ("Hotels", r"\bhotels?\b|marriott|hilton|hyatt|\bihg\b|holiday inn|best western|wyndham|sheraton|westin|"
               r"radisson|four seasons|ritz|\bw hotels?\b|kimpton|motel|resort|airbnb|vrbo|booking\.com|agoda|hostel"),
    ("Rent + Utilities", r"\brent\b|apartments?|property mgmt|mortgage|\bhoa\b|pg&e|pge|pgande|con ?ed|edison|electric|"
                         r"\bwater\b|comcast|xfinity|verizon|at&t|att\*|t-mobile|spectrum|internet|utility|sewer|power co|duke energy"),
    ("Subscriptions", r"netflix|spotify|hulu|disney ?plus|disney\+|hbo ?max|max\.com|youtube ?premium|google \*youtube|"
                      r"apple\.com/bill|apple\.com bill|itunes|google \*(storage|one)|google one|icloud|amazon prime|prime member|"
                      r"amzn prime|prime video|audible|openai|chatgpt|anthropic|claude\.ai|dropbox|adobe|microsoft 365|msft|"
                      r"nytimes|new york times|wsj|paramount|peacock|patreon|duolingo|linkedin|equinox|planet fitness|"
                      r"classpass|peloton|orangetheory|crunch fitness|24 hour fitness|la fitness|substack|notion|canva"),
    ("Fuel", r"shell|chevron|exxon|mobil\b|arco|\b76\b|bp#|\bbp\b|gas station|valero|sunoco|speedway|wawa|marathon petro|"
             r"costco gas|sam'?s fuel|tesla supercharg|chargepoint|electrify america|circle k"),
    ("Groceries", r"india(n)? ?(bazaa?r|store|grocer|grocery|market|cash|plaza|sweets)|bazaa?r|patel bro|apna|subzi|sabzi|"
                  r"\bmandi\b|\bdesi\b|namaste plaza|subji|h mart|99 ranch|mitsuwa|nijiya|seafood city|ranch market|"
                  r"whole ?foods|wholefds|trader joe|safeway|kroger|costco|wegmans|publix|sprouts|aldi|h-?e-?b\b|ralphs|"
                  r"instacart|grocery|groceries|food lion|giant eagle|stop & shop|albertsons|vons|h mart|99 ranch|amazon fresh|market"),
    ("Eating Out", r"uber ?eats|doordash|grubhub|postmates|seamless|caviar|starbucks|blue bottle|peet'?s|dunkin|philz|coffee|"
                   r"cafe|café|bakery|restaurant|grill|pizza|sushi|taco|burger|chipotle|sweetgreen|mcdonald|chick-fil-a|"
                   r"panera|shake shack|cava\b|thai|ramen|\bbar\b|kitchen|bistro|diner|tst\*|toast|eatery|boba|\btea\b"),
    ("Miscellaneous", r"\buber\b(?!\s*\*?\s*eats)|\blyft\b|venmo|atm withdrawal|\bfee\b"),
    ("Shopping", r"amazon|amzn|target|walmart|best buy|ikea|etsy|ebay|nordstrom|zara|uniqlo|h&m|sephora|ulta|nike|"
                 r"apple store|home depot|lowe'?s|wayfair|shein|temu|macy'?s|tj ?maxx|marshalls|old navy|gap\b|costco\.com"),
]
RULES = [(b, re.compile(rx, re.I)) for b, rx in RULES]
NON_SUB_BUCKETS = {"Rent + Utilities", "Groceries", "Fuel", "Eating Out", "Vehicle"}

BANK_CATEGORY_MAP = [
    (r"food|dining|restaurant", "Eating Out"), (r"grocer", "Groceries"), (r"\bgas\b|fuel", "Fuel"), (r"automotive|auto ?service|parking|insurance", "Vehicle"),
    (r"airline|air travel", "Flights"), (r"lodging|hotel", "Hotels"), (r"shopping|merchandise|retail|home", "Shopping"),
    (r"bills|utilities", "Rent + Utilities"), (r"subscription|streaming", "Subscriptions"),
]

_NOISE = re.compile(r"^(aplpay |apple pay |applepay |gpay |googlepay |sq \*|sq\*|tst\* ?|pp\*|paypal \*|sp \*|dd \*|pos |pos debit |debit card purchase |"
                    r"purchase authorized on \d\d/\d\d |recurring payment |card \d+ |checkcard \d+ )", re.I)


def merchant_key(description):
    s = _NOISE.sub("", description.lower())
    s = re.sub(r"[#*]\s*\w*\d\w*", " ", s)
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[^a-z& ]+", " ", s)
    s = re.sub(r"\b(ca|ny|tx|wa|fl|il|ma|nj|usa|us|inc|llc|com|www|ppd|id|web|ach|pmt|debit|credit)\b", " ", s)
    words = s.split()
    return " ".join(words[:3]).title() if words else description.strip()[:30]


# ---------------- classification ----------------

def _load_rows(conn):
    amap = accts_mod.by_id()
    rows = []
    for r in conn.execute("SELECT * FROM transactions ORDER BY date"):
        r = dict(r)
        a = amap.get(r["account_id"], {"type": "other", "name": r["account_id"], "institution": None})
        r["account_type"], r["account_name"], r["institution"] = a["type"], a["name"], a.get("institution")
        r["owner"] = a.get("owner")  # member id, "joint", or None (unknown)
        r["merchant"] = merchant_key(r["description"])
        rows.append(r)
    return rows


def _pair_transfers(rows):
    """Same amount out of one of your accounts and into another within 5 days = a transfer."""
    ids = set()
    ins = defaultdict(list)
    for r in rows:
        if r["amount"] > 0:
            ins[round(r["amount"], 2)].append(r)
    for r in rows:
        # Only bank-side outflows (not card purchases) of $20+ can be one half of a transfer.
        if r["amount"] > -20 or r["account_type"] not in ("checking", "savings", "other"):
            continue
        for c in ins.get(round(-r["amount"], 2), []):
            if c["account_id"] != r["account_id"] and c["id"] not in ids and \
                    abs((date.fromisoformat(c["date"]) - date.fromisoformat(r["date"])).days) <= 5:
                ids.update((r["id"], c["id"]))
                r["pair_type"], r["pair_owner"] = c["account_type"], c.get("owner")
                c["pair_type"], c["pair_owner"] = r["account_type"], r.get("owner")
                break
    return ids


def classify_all(rows):
    paired = _pair_transfers(rows)
    for r in rows:
        t, d, amt = r["account_type"], r["description"], r["amount"]
        if t == "investment":
            # 401(k) contributions (yours + employer match) are money you earned, so they count as Money in.
            r["kind"] = "retirement" if amt > 0 and RETIREMENT.search(d) else "investment_activity"
        elif t == "credit_card":
            if amt > 0:
                r["kind"] = "transfer" if CARD_PAYMENT.search(d) or r["id"] in paired else "refund"
            else:
                r["kind"] = "spend"
        else:  # checking / savings / other
            if amt < 0:
                if TO_INVEST.search(d) or r.get("pair_type") == "investment":
                    r["kind"] = "to_investments"
                elif (t != "savings" and TO_SAVINGS.search(d)) or r.get("pair_type") == "savings":
                    r["kind"] = "to_savings"
                elif r["id"] in paired:
                    r["kind"] = "transfer"
                elif TRANSFER_LIKE.search(d) and household.mentioned(d):
                    # Money to a household member's account that isn't uploaded (yet). Still household money.
                    r["kind"], r["member"] = "member_out", sorted(household.mentioned(d))[0]
                elif CARD_PAYMENT.search(d):
                    # A card bill with no matching payment on any uploaded card statement: that card's statement
                    # isn't uploaded. Counted as spending (an estimate) until it is.
                    r["kind"] = "unuploaded_card"
                elif OWN_TRANSFER.search(d):
                    r["kind"] = "transfer"
                else:
                    r["kind"] = "spend"
            else:
                if t == "savings" and INTEREST.search(d):
                    r["kind"] = "interest"
                elif r["id"] in paired:
                    r["kind"] = "transfer"
                elif TRANSFER_LIKE.search(d) and household.mentioned(d):
                    # From a household member's account that isn't uploaded (yet): money in from them.
                    r["kind"], r["member"] = "member_in", sorted(household.mentioned(d))[0]
                elif OWN_TRANSFER.search(d) or CARD_PAYMENT.search(d):
                    r["kind"] = "transfer"
                elif REFUND.search(d):
                    r["kind"] = "refund"
                else:
                    r["kind"] = "income"
    return rows


def recurring_merchants(rows):
    """Merchants charged in 2+ different months with a steady amount (likely subscriptions)."""
    by_m = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["kind"] == "spend":
            by_m[r["merchant"]][r["month"]].append(-r["amount"])
    out = {}
    for m, months in by_m.items():
        if len(months) < 2:
            continue
        amounts = [max(v) for v in months.values()]
        lo, hi = min(amounts), max(amounts)
        # One charge a month at a steady price; skip merchants a rule already places (groceries, rides, ...).
        if hi >= 2 and (hi - lo) / hi <= 0.2 and all(len(v) == 1 for v in months.values()) \
                and not any(rx.search(m) for b, rx in RULES if b != "Subscriptions"):
            out[m] = sorted(months)
    return out


def bucket_for(r, recurring, cache):
    """Strongest signal first: our rules, then Claude's verdict on this merchant, then what Claude read on the
    statement, and only then the 'charged every month at the same price' guess."""
    d = r["description"]
    for b, rx in RULES:
        if rx.search(d):
            return b
    if cache.get(r["merchant"]) in BUCKETS:
        return cache[r["merchant"]]
    if r.get("bucket_hint") in BUCKETS and r["bucket_hint"] != "Miscellaneous":
        return r["bucket_hint"]
    if r["merchant"] in recurring:
        return "Subscriptions"
    bc = r.get("bank_category") or ""
    for rx, b in BANK_CATEGORY_MAP:
        if re.search(rx, bc, re.I):
            return b
    return "Miscellaneous"


def assign_buckets(rows, cache=None):
    cache = load_json(MERCHANT_CACHE, {}) if cache is None else cache
    recurring = recurring_merchants(rows)
    for r in rows:
        if r["kind"] in ("spend", "refund"):
            r["bucket"] = bucket_for(r, recurring, cache)
    return recurring


def unknown_merchants(month):
    """Merchants in this month that only landed in Miscellaneous by default (candidates for Claude)."""
    conn = db()
    rows = classify_all(_load_rows(conn))
    cache = load_json(MERCHANT_CACHE, {})
    recurring = assign_buckets(rows, cache)
    out = {}
    for r in rows:
        if r["month"] != month or r["kind"] != "spend" or r["merchant"] in cache:
            continue
        if any(rx.search(r["description"]) for _, rx in RULES):
            continue
        unsure = r["bucket"] == "Miscellaneous" and not r.get("bucket_hint") and not r.get("bank_category")
        guessed_sub = r["bucket"] == "Subscriptions" and r["merchant"] in recurring  # repeat charge - confirm it's really a subscription
        if unsure or guessed_sub:
            out[r["merchant"]] = r["description"]
    return out


# ---------------- whose is it ----------------

def attribute(rows, amap):
    """Tag each row with who it belongs to (r["who"]): a member id, "joint", or None (unknown).
    Income landing in a joint account is matched to a person by: a name in the description, an employer that
    also pays into one person's own account, or the user's earlier answer. Nothing about employers is hard-coded."""
    names = household.member_names()
    learned = defaultdict(set)
    for r in rows:
        if r["kind"] in ("income", "retirement") and r.get("owner") in names:
            learned[income_nickname(r)].add(r["owner"])
    answers = household.income_owner_answers()
    last4_owner = {a["last4"]: a.get("owner") for a in amap.values() if a.get("last4") and a.get("owner")}
    for r in rows:
        k = r["kind"]
        if k in ("income", "interest", "retirement"):
            if r.get("owner") in names:
                r["who"] = r["owner"]
            else:
                ment = household.mentioned(r["description"])
                payer = income_nickname(r)
                if len(ment) == 1:
                    r["who"] = next(iter(ment))
                elif len(learned.get(payer, ())) == 1:
                    r["who"] = next(iter(learned[payer]))
                else:
                    r["who"] = answers.get(payer)
        elif k in ("member_in", "member_out"):
            r["who"] = r["member"]
        elif k == "unuploaded_card":
            m = CARD_LAST4.search(r["description"])
            r["card_last4"] = m.group(1) if m else None
            r["who"] = last4_owner.get(r["card_last4"]) if m else None
        else:
            r["who"] = r.get("owner")
    return rows


# ---------------- nicknames ----------------

def income_nickname(r):
    d = r["description"]
    if r.get("kind") == "member_in":
        return f"From {household.member_names().get(r['member'], r['member'])}'s accounts (not uploaded)"
    if r.get("kind") == "retirement":
        return "401(k) employer match" if EMPLOYER.search(d) else "401(k) your contribution"
    m = re.search(r"^(.*?)\b(payroll|direct dep|dir dep|salary|paycheck|ppd|reg\.? salary)\b", d, re.I)
    if re.search(r"payroll|direct dep|dir dep|salary|paycheck|gusto|adp", d, re.I):
        company = merchant_key(m.group(1)) if m and m.group(1).strip() else ""
        company = " ".join(w for w in company.split() if w.lower() not in {"lp", "llc", "inc", "corp", "co", "ltd", "a", "del", "us"})
        return f"{company} salary" if company else "Salary"
    if INTEREST.search(d):
        return f"{r.get('institution') or r['account_name']} interest"
    if re.search(r"dividend", d, re.I):
        return "Dividends"
    if re.search(r"irs|tax ref|treas 310", d, re.I):
        return "Tax refund"
    m = re.search(r"zelle\W*(?:payment\s+)?from\s+([A-Za-z][A-Za-z .'-]{1,40}?)(?:\s+(?:on|conf|ref|id)\b|\s+\w*\d|$)", d, re.I)
    if m:
        # Drop middle initials so "Jane Q Doe" and "Jane Doe" are one source.
        name = " ".join(w for w in m.group(1).split() if len(w.strip(".")) > 1)
        return f"Zelle from {name.title()}"
    if re.search(r"venmo", d, re.I):
        return "Venmo transfer in"
    if re.search(r"paypal", d, re.I):
        return "PayPal transfer in"
    if re.search(r"cash ?back|reward", d, re.I):
        return "Cash back"
    return merchant_key(d)


# ---------------- report ----------------

def _tx(r):
    return {"date": r["date"], "account": r["account_name"], "description": r["description"], "amount": round(r["amount"], 2)}


def _cadence(merchant, month, seen):
    months = seen.get(merchant, [])
    if not months:
        return "new"
    y, m = map(int, month.split("-"))
    prev = f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"
    year_ago = f"{y - 1:04d}-{m:02d}"
    if prev in months:
        return "monthly"
    if year_ago in months:
        return "annual"
    return "recurring" if len([x for x in months if x < month]) else "new"


_MONTH_WORDS = re.compile(r"\b(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t|tember)?|oct(ober)?|"
                         r"nov(ember)?|dec(ember)?|statement|20\d\d)\b", re.I)


def statements(conn, account, month):
    """Every statement for this account whose period ends in `month`, each with a display label."""
    start, end = month_bounds(month)
    out = []
    for st in conn.execute("SELECT s.*, f.name AS file_name FROM summaries s LEFT JOIN files f ON f.hash = s.file_hash "
                           "WHERE s.account_id = ? AND s.period_end >= ? AND s.period_end < ? ORDER BY s.period_end",
                           (account["id"], start, end)):
        st = dict(st)
        label = st.get("label")
        if not label and st.get("file_name"):
            label = " ".join(_MONTH_WORDS.sub(" ", st["file_name"].rsplit(".", 1)[0]).split()).strip(" -_") or None
        st["label"] = label or account["name"]
        out.append(st)
    return out


def coverage_gaps(conn, amap, month, exact_only=False):
    """Accounts whose uploaded statements don't cover the whole calendar month (statement cycles differ).
    exact_only: use only periods printed on statements, ignoring ones inferred from CSV transaction dates."""
    start, end = month_bounds(month)
    last_day = (date.fromisoformat(end) - timedelta(days=1)).isoformat()
    periods = defaultdict(list)
    for s in conn.execute("SELECT account_id, period_start, period_end, approx FROM summaries WHERE period_start IS NOT NULL "
                          "AND period_end IS NOT NULL AND period_end >= ? AND period_start <= ?" + (" AND approx = 0" if exact_only else ""),
                          (start, last_day)):
        a, b = s["period_start"], s["period_end"]
        if s["approx"]:  # CSV: dates are first/last transaction, so allow a few quiet days at the edges
            if (date.fromisoformat(a) - date.fromisoformat(start)).days <= 4:
                a = min(a, start)
            if (date.fromisoformat(last_day) - date.fromisoformat(b)).days <= 4:
                b = max(b, last_day)
        periods[s["account_id"]].append((a, b, s["approx"]))
    out = []
    for acct_id, ps in periods.items():
        ps.sort()
        covered_from, covered_to, prev_approx = ps[0]
        for a, b, approx in ps[1:]:
            slack = 5 if (approx or prev_approx) else 1  # quiet days between CSV exports aren't a gap
            if a <= (date.fromisoformat(covered_to) + timedelta(days=slack)).isoformat():
                covered_to = max(covered_to, b)
                prev_approx = approx
        name = amap.get(acct_id, {}).get("name", acct_id)
        missing = []
        if covered_from > start:
            missing.append({"from": start, "to": (date.fromisoformat(covered_from) - timedelta(days=1)).isoformat(), "need": "previous"})
        if covered_to < last_day:
            missing.append({"from": (date.fromisoformat(covered_to) + timedelta(days=1)).isoformat(), "to": last_day, "need": "next"})
        if missing:
            out.append({"account": name, "missing": missing})
    return out, set(periods)


def build(month, view="household"):
    """The report for one calendar month. view = "household" or a member id (e.g. "alex").
    Household: everything. Person: their income (incl. their share of joint deposits), spending on their own
    cards/accounts, their savings and investments. Shared (joint-account) spending stays in the household view."""
    conn = db()
    amap = accts_mod.by_id()
    rows = attribute(classify_all(_load_rows(conn)), amap)
    recurring = assign_buckets(rows)
    mrows = [r for r in rows if r["month"] == month]
    files = [dict(f) for f in conn.execute("SELECT name, account_id, status, method, note FROM files WHERE month = ?", (month,))]
    start, end = month_bounds(month)
    # Strict calendar month: the report covers every transaction dated in this month, from any statement.
    # Before this month's own statements are uploaded, it shows the leftovers from neighbouring statements.
    uploaded = any(f["status"] == "read" for f in files)
    if not mrows and not uploaded:
        return {"month": month, "view": view, "has_data": False, "files": files}
    upload_month = {r["hash"]: r["month"] for r in conn.execute("SELECT hash, month FROM files")}
    leftovers = defaultdict(int)
    for r in mrows:
        um = upload_month.get(r["file_hash"])
        if um and um != month:
            leftovers[um] += 1

    names = household.member_names()
    person = view if view in names else None
    mine = (lambda r: r.get("who") == person) if person else (lambda r: True)
    my_accounts = [a for a in amap.values() if not person or a.get("owner") == person]

    # Income
    src = defaultdict(list)
    for r in mrows:
        if r["kind"] in ("income", "interest", "retirement", "member_in") and mine(r):
            src[income_nickname(r)].append(r)
    income_total = round(sum(r["amount"] for v in src.values() for r in v), 2)
    sources = sorted(({"nickname": k, "total": round(sum(r["amount"] for r in v), 2), "count": len(v),
                       "who": names.get(v[0].get("who")) if not person else None,
                       "accounts": sorted({r["account_name"] for r in v}), "transactions": [_tx(r) for r in v]}
                      for k, v in src.items()), key=lambda s: -s["total"])

    # Spending: each transaction in exactly one bucket; refunds net against their bucket.
    # Card bills for cards whose statement isn't uploaded are an estimate, shown as their own line.
    by_bucket = {b: [] for b in BUCKETS}
    estimates = []
    for r in mrows:
        if r["kind"] in ("spend", "refund") and mine(r):
            by_bucket[r["bucket"]].append(r)
        elif r["kind"] == "unuploaded_card" and mine(r):
            estimates.append(r)
    buckets = []
    for b in BUCKETS:
        v = by_bucket[b]
        total = round(-sum(r["amount"] for r in v), 2)
        buckets.append({"name": b, "total": total, "count": len(v),
                        "pct_of_income": round(total / income_total * 100, 1) if income_total else None,
                        "transactions": [_tx(r) for r in sorted(v, key=lambda x: x["amount"])]})
    if estimates:
        total = round(-sum(r["amount"] for r in estimates), 2)
        buckets.append({"name": "Card bills (statement not uploaded)", "estimate": True, "total": total, "count": len(estimates),
                        "pct_of_income": round(total / income_total * 100, 1) if income_total else None,
                        "transactions": [_tx(r) for r in estimates]})
    spend_total = round(sum(b["total"] for b in buckets), 2)
    shared_spend = round(-sum(r["amount"] for r in mrows if r["kind"] in ("spend", "refund") and r.get("who") == "joint"), 2)

    # Subscriptions
    subs = defaultdict(list)
    for r in by_bucket["Subscriptions"]:
        subs[r["merchant"]].append(r)
    prev_month, seen = {}, defaultdict(set)
    for r in rows:
        if r.get("bucket") == "Subscriptions" and r["kind"] == "spend":
            seen[r["merchant"]].add(r["month"])
            if r["month"] < month:
                prev_month[r["merchant"]] = -r["amount"]
    sub_items = []
    for m, v in subs.items():
        amt = round(-sum(r["amount"] for r in v), 2)
        last = prev_month.get(m)
        change = round(amt - last, 2) if last and abs(amt - last) >= 0.5 and len(v) == 1 else None
        sub_items.append({"name": m, "amount": amt, "count": len(v), "cadence": _cadence(m, month, seen),
                          "change": change, "transactions": [_tx(r) for r in v]})
    sub_items.sort(key=lambda s: -s["amount"])

    # Liquid savings. "Saved" = how much the savings balance grew: money that landed there and wasn't spent or moved out.
    savings_rows, saved_total = [], 0.0
    for a in my_accounts:
        if a["type"] != "savings":
            continue
        ar = [r for r in mrows if r["account_id"] == a["id"]]
        stmts = statements(conn, a, month)
        if not ar and not stmts:
            continue
        flow_amt = round(sum(r["amount"] for r in ar), 2)
        tx_interest = round(sum(r["amount"] for r in ar if r["kind"] == "interest"), 2)
        for st in stmts or [None]:
            if st is None:
                savings_rows.append({"name": a["name"], "start_balance": None, "end_balance": None, "saved": flow_amt,
                                     "interest": tx_interest, "apy_estimate": None})
            else:
                grew = round(st["end_balance"] - st["start_balance"], 2) if st["start_balance"] is not None and st["end_balance"] is not None else None
                interest = st["interest"] if st["interest"] is not None else (tx_interest if len(stmts) == 1 else None)
                avg = (st["start_balance"] + st["end_balance"]) / 2 if grew is not None else None
                savings_rows.append({"name": st["label"], "start_balance": st["start_balance"], "end_balance": st["end_balance"],
                                     "saved": grew if grew is not None else (flow_amt if len(stmts) == 1 else None), "interest": interest,
                                     "apy_estimate": round(interest / avg * 12 * 100, 2) if avg and interest else None})
            saved_total += savings_rows[-1]["saved"] or 0
    saved_total = round(saved_total, 2)

    # Investments: one row per statement (e.g. 401(k) and Individual are separate statements).
    invest_rows = []
    for a in my_accounts:
        if a["type"] != "investment":
            continue
        ar = [r for r in mrows if r["account_id"] == a["id"]]
        stmts = statements(conn, a, month)
        if not stmts and ar:
            contrib = round(sum(r["amount"] for r in ar if r["amount"] > 0 and not re.search(r"dividend|interest|reinvest", r["description"], re.I)), 2)
            invest_rows.append({"name": a["name"], "start_value": None, "end_value": None, "contributions": contrib,
                                "market_change": None, "roi_pct": None})
        for st in stmts:
            contrib, withdrawn = st["contributions"] or 0, st["withdrawals"] or 0
            sv, ev = st["start_balance"], st["end_balance"]
            change = round(ev - sv - contrib + withdrawn, 2) if sv is not None and ev is not None else None
            invest_rows.append({"name": st["label"], "start_value": sv, "end_value": ev, "contributions": contrib,
                                "market_change": change, "roi_pct": round(change / sv * 100, 2) if change is not None and sv else None})
    # Money moved into investments: attributed to whoever owns the investment account it went to.
    to_inv = [r for r in mrows if r["kind"] == "to_investments" and (not person or (r.get("pair_owner") or r.get("owner")) == person)]
    sent_to_invest = round(-sum(r["amount"] for r in to_inv), 2)
    retirement = round(sum(r["amount"] for r in mrows if r["kind"] == "retirement" and mine(r)), 2)
    invested_flow = round(sent_to_invest + retirement, 2)
    with_change = [a for a in invest_rows if a["market_change"] is not None and a["start_value"]]

    # Who put money into joint accounts this month (computed from deposits, so any split works).
    to_joint = defaultdict(float)
    for r in mrows:
        if r.get("owner") != "joint" or r["amount"] <= 0:
            continue
        if r["kind"] in ("income", "member_in"):
            to_joint[r.get("who") or "unknown"] += r["amount"]
        elif r["kind"] == "transfer" and r.get("pair_owner") in names:
            to_joint[r["pair_owner"]] += r["amount"]
    contributions = [{"who": names.get(k, "Not sure yet"), "id": k, "amount": round(v, 2)} for k, v in sorted(to_joint.items(), key=lambda x: -x[1])]

    left_over = round(income_total - spend_total, 2)
    # Cash = savings (HYSA) growth + change in checking. Checking change comes straight from its transactions.
    chk_rows = [r for r in mrows if r["account_type"] in ("checking", "other") and (not person or r.get("owner") == person)]
    checking_change = round(sum(r["amount"] for r in chk_rows), 2)
    if person:
        liquid = round(saved_total + checking_change, 2)
        flow = {"view": "person", "money_in": income_total, "retirement": retirement, "spent": spend_total,
                "left_over": left_over, "liquid": liquid, "saved": saved_total, "checking": checking_change,
                "invested": invested_flow, "shared_spend": shared_spend, "to_joint": round(to_joint.get(person, 0), 2),
                "savings_rate": round((liquid + invested_flow) / income_total * 100, 1) if income_total else None}
    else:
        # Money in - Spent = Left over, and it always splits exactly into three:
        #   Liquid savings = HYSA growth + checking change (+ cash moved to a member's not-uploaded account)
        #   Invested       = transfers into investment accounts + 401(k)
        #   Card bills     = card payments this month minus card charges this month (timing: paying last month's bill)
        member_out = [r for r in mrows if r["kind"] == "member_out"]
        card_timing = round(sum(r["amount"] for r in mrows if r["account_type"] == "credit_card"), 2)
        liquid_parts = [{"label": "Savings (HYSA)", "amount": saved_total},
                        {"label": "Checking", "amount": checking_change}]
        for mid in sorted({r["member"] for r in member_out}):
            amt = round(-sum(r["amount"] for r in member_out if r["member"] == mid), 2)
            liquid_parts.append({"label": f"Moved to {names.get(mid, mid)}'s accounts (not uploaded)", "amount": amt})
        uses = [
            {"key": "liquid", "label": "Liquid savings", "amount": round(sum(p["amount"] for p in liquid_parts), 2),
             "parts": liquid_parts},
            {"key": "invested", "label": "Invested", "amount": invested_flow,
             "parts": [{"label": "Transfers to investment accounts", "amount": sent_to_invest},
                       {"label": "401(k) (yours + employer)", "amount": retirement}]},
            {"key": "cards", "label": "Card bills", "amount": card_timing,
             "parts": [{"label": "Card payments this month", "amount": round(sum(r["amount"] for r in mrows if r["account_type"] == "credit_card" and r["amount"] > 0 and r["kind"] == "transfer"), 2)},
                       {"label": "Card charges this month", "amount": round(sum(r["amount"] for r in mrows if r["account_type"] == "credit_card" and not (r["amount"] > 0 and r["kind"] == "transfer")), 2)}]},
        ]
        flow = {"view": "household", "money_in": income_total, "retirement": retirement, "spent": spend_total,
                "left_over": left_over, "uses": uses, "contributions": contributions,
                "savings_rate": round(left_over / income_total * 100, 1) if income_total else None,
                "unexplained": round(left_over - sum(u["amount"] for u in uses), 2)}

    # Things only you can answer (asked once, remembered): whose income is this / whose account is this.
    needs_input = []
    if names:
        unknown_income = defaultdict(float)
        for r in mrows:
            if r["kind"] in ("income", "retirement") and not r.get("who"):
                unknown_income[income_nickname(r)] += r["amount"]
        for payer, amt in sorted(unknown_income.items(), key=lambda x: -x[1]):
            needs_input.append({"type": "income", "key": payer, "text": f"Whose income is “{payer}” ({amt:,.2f} this month)?"})
        for aid in sorted({r["account_id"] for r in mrows if not r.get("owner")}):
            needs_input.append({"type": "account", "key": aid, "text": f"Whose account is “{amap.get(aid, {}).get('name', aid)}”?"})

    unreadable = [f["name"] for f in files if f["status"] == "unreadable"]
    # Complete = this month AND the months before and after were uploaded (statement cycles never shift by more
    # than a month), and no statement-printed period shows a hole (e.g. one card's next statement wasn't uploaded).
    y, m = map(int, month.split("-"))
    prev_m = f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"
    next_m = f"{y + (m == 12):04d}-{m % 12 + 1:02d}"
    up = {r[0] for r in conn.execute("SELECT DISTINCT month FROM files WHERE status = 'read'")}
    needed = [x for x in (prev_m, next_m) if x not in up]
    if not uploaded:
        gaps = []
    elif needed:
        gaps = coverage_gaps(conn, amap, month)[0]
    else:
        gaps = coverage_gaps(conn, amap, month, exact_only=True)[0]
    complete = uploaded and not needed and not gaps
    not_uploaded_cards = sorted({r.get("card_last4") or "?" for r in mrows if r["kind"] == "unuploaded_card"})
    return {
        "month": month, "view": view, "members": [{"id": k, "name": v} for k, v in names.items()],
        "has_data": True, "files": files, "unreadable": unreadable, "gaps": gaps, "uploaded": uploaded, "complete": complete,
        "months_needed": needed, "needs_input": needs_input, "cards_not_uploaded": not_uploaded_cards,
        "leftover_from": [{"month": m, "count": n} for m, n in sorted(leftovers.items())],
        "spillover": sorted({r["month"] for r in rows if r["file_hash"] in {x["hash"] for x in conn.execute(
            "SELECT hash FROM files WHERE month = ?", (month,))} and r["month"] != month}),
        "totals": {"income": income_total, "spending": spend_total, "left_over": left_over,
                   "saved": saved_total, "invested": invested_flow},
        "flow": flow,
        "income": {"total": income_total, "sources": sources},
        "spending": {"total": spend_total, "pct_of_income": round(spend_total / income_total * 100, 1) if income_total else None,
                     "buckets": buckets, "shared": shared_spend if person else None},
        "subscriptions": {"total": round(sum(s["amount"] for s in sub_items), 2), "items": sub_items},
        "savings": {"accounts": savings_rows, "saved": saved_total,
                    "checking": [{"name": a["name"], "change": round(sum(r["amount"] for r in chk_rows if r["account_id"] == a["id"]), 2)}
                                 for a in my_accounts if a["type"] in ("checking", "other") and any(r["account_id"] == a["id"] for r in chk_rows)],
                    "checking_change": checking_change, "liquid": round(saved_total + checking_change, 2),
                    "interest": round(sum(a["interest"] or 0 for a in savings_rows), 2),
                    "end_balance": round(sum(a["end_balance"] for a in savings_rows if a["end_balance"] is not None), 2)
                    if any(a["end_balance"] is not None for a in savings_rows) else None},
        "investments": {"accounts": invest_rows, "contributions": invested_flow, "sent_from_bank": sent_to_invest,
                        "pct_of_income": round(invested_flow / income_total * 100, 1) if income_total and invested_flow else None,
                        "roi_pct": round(sum(a["market_change"] for a in with_change) / sum(a["start_value"] for a in with_change) * 100, 2)
                        if with_change else None,
                        "end_value": round(sum(a["end_value"] for a in invest_rows if a["end_value"] is not None), 2)
                        if any(a["end_value"] is not None for a in invest_rows) else None},
        "suggestions": load_json(DATA / month / "suggestions.json") if not person else None,
    }


def facts_for_suggestions(rep):
    """Aggregates only - this is all Claude sees when writing suggestions."""
    top = lambda b: [{"merchant": merchant_key(t["description"]), "amount": -t["amount"]} for t in b["transactions"][:5]]  # noqa: E731
    return {
        "month": rep["month"], "totals": rep["totals"],
        "spending_buckets": [{"bucket": b["name"], "total": b["total"], "pct_of_income": b["pct_of_income"], "transactions": b["count"],
                              "largest": top(b)} for b in rep["spending"]["buckets"] if b["total"]],
        "subscriptions": [{"name": s["name"], "amount": s["amount"], "cadence": s["cadence"], "price_change": s["change"]}
                          for s in rep["subscriptions"]["items"]],
        "household": household.context(),
        "money_flow": {**{k: rep["flow"][k] for k in ("money_in", "retirement", "spent", "left_over", "savings_rate")},
                       "where_it_went": {u["label"]: u["amount"] for u in rep["flow"].get("uses", [])},
                       "put_into_joint_account": {c["who"]: c["amount"] for c in rep["flow"].get("contributions", [])},
                       "note": "money_in includes 401(k) contributions (own + employer match). Lines marked 'not uploaded' "
                               "or 'estimate' come from statements not uploaded yet - mention uploading them if large. "
                               "How much each partner puts into the joint account is their deliberate choice - don't comment on the split."},
        "savings": {"saved_this_month": rep["savings"]["saved"], "interest": rep["savings"]["interest"],
                    "note": "saved = growth of high-yield savings balances"},
        "investing": {"contributions": rep["investments"]["contributions"], "pct_of_income": rep["investments"]["pct_of_income"],
                      "roi_pct": rep["investments"]["roi_pct"]},
    }


def rule_suggestions(rep):
    """Used only when Claude isn't available: up to 6 simple cards from the numbers."""
    inc = rep["totals"]["income"] or 0
    items = []
    bs = sorted([b for b in rep["spending"]["buckets"] if b["total"] > 0 and b["name"] != "Rent + Utilities"
                 and not b.get("estimate")], key=lambda b: -b["total"])
    for b in bs[:3]:
        pct = f" ({b['pct_of_income']:.0f}% of money in)" if b["pct_of_income"] is not None else ""
        items.append({"title": f"Trim {b['name']}", "detail": f"{b['name']} was ${b['total']:,.0f}{pct} across {b['count']} transactions.",
                      "impact": f"≈ ${b['total'] * 0.2:,.0f}/mo if cut 20%", "type": "cut"})
    subs = rep["subscriptions"]
    if subs["items"]:
        top = subs["items"][0]
        items.append({"title": "Review subscriptions", "detail": f"{len(subs['items'])} subscriptions cost ${subs['total']:,.0f}; biggest is {top['name']} (${top['amount']:,.0f}).",
                      "impact": None, "type": "watch"})
    est = [b for b in rep["spending"]["buckets"] if b.get("estimate")]
    if est:
        items.append({"title": "Upload missing card statements", "detail": f"${est[0]['total']:,.0f} of card bills can't be broken down yet.",
                      "impact": None, "type": "action"})
    if inc:
        rate = rep["flow"].get("savings_rate")
        items.append({"title": "Savings rate", "detail": f"You kept {rate:.0f}% of money in this month; 20%+ is a solid target.",
                      "impact": None, "type": "good" if (rate or 0) >= 20 else "watch"})
    return items[:6]
