#!/usr/bin/env python3
"""
Colorado UCC-1 collector - pilot for MCA lead generation.

Pulls fresh UCC-1 initial financing statements from the Colorado Secretary of
State open data portal (data.colorado.gov, Socrata SODA API), joins them with
debtor and secured-party records, and optionally resolves a human decision
maker for corporate debtors via the CO business entity registry.

Datasets used (all public, no key required; a free app token lifts rate limits):
    wffy-3uut  UCC Filing Information      - 2.57M rows, 1966..today
    8upq-58vz  UCC Debtor Information
    ap62-sav4  Secured Party Information
    4ykn-tg5h  Business Entities in Colorado (registered agent -> LPR)

Usage:
    python3 ucc_colorado.py --limit 5
    python3 ucc_colorado.py --limit 500 --days 14 --enrich --out leads.json
    APP_TOKEN=xxxx python3 ucc_colorado.py --limit 1000 --days 7 --enrich

Standard library only. Python 3.8+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DOMAIN = "https://data.colorado.gov/resource"
DS_FILING = "wffy-3uut"
DS_DEBTOR = "8upq-58vz"
DS_SECURED = "ap62-sav4"
DS_ENTITY = "4ykn-tg5h"

# Socrata caps a single page at 50k rows; we stay well under and page anyway.
PAGE = 1000
# `fileid in(...)` is sent as a URL query, so the id batch must stay short
# enough that the resulting URL does not get rejected.
JOIN_BATCH = 400

USER_AGENT = "ucc-co-pilot/1.0"
TIMEOUT = 30
RETRIES = 4

# Name suffixes that Colorado clerks routinely type into the lastname column.
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "md", "dds", "esq"}

# Corporate noise stripped before matching a debtor name against the registry.
ENTITY_NOISE = re.compile(
    r"\b(l\.?l\.?c|l\.?l\.?p|l\.?p|inc|incorporated|corp|corporation|co|company|"
    r"ltd|limited|pllc|pc|p\.c|plc|trust|assoc|association)\b\.?",
    re.I,
)

# Filers that appear as the secured party but are only acting for someone else.
# MCA funders routinely file through these, so a name filter alone misses them.
REPRESENTATIVE_FILERS = re.compile(
    r"\b(as\s+representative|c\s?t\s+corporation|corporation\s+service\s+company|"
    r"csc\b|cogency|first\s+corporate\s+solutions|lien\s+solutions|wolters\s+kluwer)\b",
    re.I,
)


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def soql(dataset: str, params: dict) -> list:
    """Run one SoQL query with retry on throttling and transient errors."""
    token = os.environ.get("APP_TOKEN", "").strip()
    url = f"{DOMAIN}/{dataset}.json?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token:
        req.add_header("X-App-Token", token)

    delay = 1.0
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 429 = throttled without an app token, 5xx = portal hiccup.
            if exc.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            body = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"HTTP {exc.code} on {dataset}: {body}") from exc
        except urllib.error.URLError:
            if attempt < RETRIES - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return []


def q(value: str) -> str:
    """Quote a SoQL string literal. Doubling the apostrophe matters here:
    real debtor names look like Angelo's Taverna LLC and an unescaped quote
    breaks the query (or worse, injects into it)."""
    return "'" + str(value).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def person_name(rec: dict) -> str:
    """Rebuild a person name, fixing the common case where the suffix was
    keyed into lastname (seen live: lastname='Jr.', first='Mauricio',
    middle='Garcia' should read Mauricio Garcia Jr.)."""
    first = clean(rec.get("firstname"))
    middle = clean(rec.get("middlename"))
    last = clean(rec.get("lastname"))
    suffix = clean(rec.get("suffix"))

    if last.lower().rstrip(".") in SUFFIXES and middle:
        last, suffix = middle, last
        middle = ""

    parts = [p for p in (first, middle, last) if p]
    name = " ".join(parts)
    if suffix:
        name = f"{name} {suffix}".strip()
    return name


def party_name(rec: dict) -> str:
    """A UCC party is either an organisation or a natural person."""
    org = clean(rec.get("organizationname"))
    return org if org else person_name(rec)


def address(rec: dict) -> str:
    street = " ".join(p for p in (clean(rec.get("address1")), clean(rec.get("address2"))) if p)
    city = clean(rec.get("city"))
    state = clean(rec.get("state"))
    zipc = clean(rec.get("zipcode"))
    if clean(rec.get("zipcode4")):
        zipc = f"{zipc}-{clean(rec['zipcode4'])}"
    tail = " ".join(p for p in (city + "," if city else "", state, zipc) if p)
    return ", ".join(p for p in (street, tail.strip()) if p)


def creditor_key(name: str) -> str:
    """Fold a creditor name to a match key. The portal holds IRS,
    'INTERNAL REVENUE SERVICE' and 'Internal Revenue Service' as three
    separate strings; the same happens to every MCA funder, so any
    name-based filter has to compare on a folded key, not raw text."""
    key = ENTITY_NOISE.sub(" ", (name or "").lower())
    key = re.sub(r"[^a-z0-9 ]+", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def is_organisation(rec: dict) -> bool:
    return bool(clean(rec.get("organizationname")))


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

def fetch_filings(limit: int, days: int) -> list:
    """Fresh UCC-1 only.

    The filing table mixes UCC financing statements with IRS tax liens,
    hospital liens and farm-product EFS records, and mixes initial filings
    with amendments, continuations and terminations. Without all three
    predicates roughly half the rows are not UCC-1 at all.
    """
    where = [
        "filingtype='ucc'",
        "documenttype='UCC financing statement'",
        "transactiontype='Initial Filing'",
    ]
    if days:
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        where.append(f"filingdate >= '{since}T00:00:00.000'")

    rows, offset = [], 0
    while len(rows) < limit:
        page = soql(DS_FILING, {
            "$select": "fileid,filingdate,masterdocumentid,lapsedate",
            "$where": " AND ".join(where),
            "$order": "filingdate DESC, fileid DESC",
            "$limit": min(PAGE, limit - len(rows)),
            "$offset": offset,
        })
        if not page:
            break
        rows.extend(page)
        offset += len(page)
        if len(page) < PAGE:
            break
    return rows[:limit]


def fetch_by_fileid(dataset: str, file_ids: list) -> dict:
    """Batch join on fileid. One request per 400 ids instead of one request
    per filing - at 500 leads/day that is the difference between 3 calls and
    1500, which is what gets an IP throttled."""
    out = {}
    for i in range(0, len(file_ids), JOIN_BATCH):
        chunk = file_ids[i:i + JOIN_BATCH]
        page = soql(dataset, {
            "$where": "fileid in(" + ",".join(str(int(f)) for f in chunk) + ")",
            "$limit": 50000,
        })
        for rec in page:
            # Keep only live rows; terminated/removed parties still sit in the table.
            if clean(rec.get("recordstatus")).lower() not in ("", "active"):
                continue
            out.setdefault(str(rec.get("fileid")), []).append(rec)
    return out


def resolve_owners(names: list) -> dict:
    """Answer to 'the debtor is an LLC, where does the human come from'.

    The CO registry publishes the registered agent for every entity. For
    small business - which is exactly the MCA borrower profile - that agent
    is usually the owner. It is a lead, not proof: national agents such as
    CT Corporation are flagged rather than returned as a person.
    """
    found = {}
    uniq = sorted({n for n in names if n})
    for i in range(0, len(uniq), 50):
        chunk = uniq[i:i + 50]
        page = soql(DS_ENTITY, {
            "$select": ("entityname,entitystatus,entitytype,entityformdate,"
                        "agentfirstname,agentmiddlename,agentlastname,agentsuffix,"
                        "agentorganizationname,principalcity"),
            "$where": "upper(entityname) in(" + ",".join(q(n.upper()) for n in chunk) + ")",
            "$limit": 5000,
        })
        for rec in page:
            agent_org = clean(rec.get("agentorganizationname"))
            person = person_name({
                "firstname": rec.get("agentfirstname"),
                "middlename": rec.get("agentmiddlename"),
                "lastname": rec.get("agentlastname"),
                "suffix": rec.get("agentsuffix"),
            })
            if agent_org and REPRESENTATIVE_FILERS.search(agent_org):
                owner, source = "", "commercial_agent"
            elif person:
                owner, source = person, "registered_agent"
            elif agent_org:
                owner, source = "", "agent_is_organisation"
            else:
                owner, source = "", "not_published"
            found[clean(rec.get("entityname")).upper()] = {
                "owner_name": owner,
                "owner_source": source,
                "entity_status": clean(rec.get("entitystatus")),
                "entity_type": clean(rec.get("entitytype")),
                "entity_formed": clean(rec.get("entityformdate"))[:10],
            }
    return found


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def build(limit: int, days: int, enrich: bool) -> list:
    filings = fetch_filings(limit, days)
    if not filings:
        return []

    file_ids = [f["fileid"] for f in filings]
    debtors = fetch_by_fileid(DS_DEBTOR, file_ids)
    secured = fetch_by_fileid(DS_SECURED, file_ids)

    leads = []
    for f in filings:
        fid = str(f["fileid"])
        for d in debtors.get(fid, []):
            sp_recs = secured.get(fid, [])
            sp_name = party_name(sp_recs[0]) if sp_recs else ""
            leads.append({
                "filing_date": clean(f.get("filingdate"))[:10],
                "debtor_name": party_name(d),
                "secured_party_name": sp_name,
                "business_address": address(d),
                # everything below is extra context, not part of the required four
                "debtor_type": "organization" if is_organisation(d) else "individual",
                "secured_party_key": creditor_key(sp_name),
                "secured_party_via_representative": bool(REPRESENTATIVE_FILERS.search(sp_name)),
                "file_id": fid,
                "master_document_id": clean(f.get("masterdocumentid")),
                "lapse_date": clean(f.get("lapsedate"))[:10],
                "source_url": f"https://www.coloradosos.gov/biz/UCCSearchCriteria.do?fileId={fid}",
            })

    if enrich:
        org_names = [l["debtor_name"] for l in leads if l["debtor_type"] == "organization"]
        registry = resolve_owners(org_names)
        for lead in leads:
            hit = registry.get(lead["debtor_name"].upper())
            if hit:
                lead.update(hit)
            elif lead["debtor_type"] == "individual":
                # Sole proprietors carry the human name in the UCC record itself.
                lead.update({"owner_name": lead["debtor_name"], "owner_source": "ucc_debtor"})
            else:
                lead.update({"owner_name": "", "owner_source": "no_registry_match"})

    return leads


def main() -> int:
    ap = argparse.ArgumentParser(description="Colorado UCC-1 collector")
    ap.add_argument("--limit", type=int, default=5, help="how many filings to pull")
    ap.add_argument("--days", type=int, default=0, help="only filings from the last N days (0 = newest first)")
    ap.add_argument("--enrich", action="store_true", help="resolve owner via CO business registry")
    ap.add_argument("--out", default="", help="write JSON here instead of stdout")
    args = ap.parse_args()

    try:
        leads = build(args.limit, args.days, args.enrich)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(leads, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(f"{len(leads)} leads -> {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
