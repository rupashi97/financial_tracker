"""Who's in the household, and whose is what.

Everything here is data-driven and learned from statements:
  data/household.json      members and the names they appear under on statements (edit freely)
  data/income_owners.json  answers to "whose income is this?" for income landing in a joint account
Account owners live in data/accounts.json ("owner": member id | "joint"), detected from the names on statements.
With no household.json, the app works as a single-person tracker.
"""
import re

from store import DATA, load_json, save_json

HOUSEHOLD = DATA / "household.json"
INCOME_OWNERS = DATA / "income_owners.json"


def title():
    return load_json(HOUSEHOLD, {}).get("title") or "Financial Tracker"


def context():
    """Optional one-line description of the household, given to Claude when it writes suggestions."""
    return load_json(HOUSEHOLD, {}).get("context") or "a household; numbers are for everyone combined"


def members():
    return load_json(HOUSEHOLD, {"members": []}).get("members", [])


def member_names():
    return {m["id"]: m["name"] for m in members()}


def _alias_patterns():
    out = []
    for m in members():
        for a in [m["name"]] + m.get("aliases", []):
            a = a.strip()
            if len(a) >= 3:
                out.append((m["id"], re.compile(r"\b" + r"\s+(?:\w\.?\s+)?".join(map(re.escape, a.split())) + r"\b", re.I)))
    return out


def mentioned(text):
    """Member ids whose name appears in the text (e.g. 'Zelle payment from Jane Q Doe')."""
    return {mid for mid, rx in _alias_patterns() if rx.search(text or "")}


def owner_from_holders(holders):
    """Account owner from the account-holder names printed on a statement."""
    found = set()
    for h in holders or []:
        found |= mentioned(h)
    if len(found) >= 2:
        return "joint"
    return next(iter(found)) if found else None


def income_owner_answers():
    return load_json(INCOME_OWNERS, {})


def set_income_owner(payer, owner):
    answers = income_owner_answers()
    answers[payer] = owner
    save_json(INCOME_OWNERS, answers)
