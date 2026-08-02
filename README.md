# Colorado UCC-1 collector

Pilot script for collecting fresh UCC-1 initial financing statements from the
Colorado Secretary of State open data portal and resolving a human decision
maker for corporate debtors.

Standard library only, no dependencies, Python 3.8+.

```bash
python3 ucc_colorado.py --limit 5 --enrich                       # demo
python3 ucc_colorado.py --limit 2000 --days 14 --enrich --out leads.json
APP_TOKEN=xxx python3 ucc_colorado.py --limit 5000 --days 7 --enrich
```

## Output

Required fields plus context:

```json
{
  "filing_date": "2026-07-31",
  "debtor_name": "Cohen Enterprises LLC",
  "secured_party_name": "Woozle Investments, LLC",
  "business_address": "1723 W Colorado Ave, Colorado Springs, CO 80904",
  "debtor_type": "organization",
  "owner_name": "JACOB MITCHELL COHEN",
  "owner_source": "registered_agent",
  "entity_status": "Good Standing",
  "entity_formed": "2024-02-28",
  "secured_party_via_representative": false,
  "source_url": "https://www.coloradosos.gov/biz/UCCSearchCriteria.do?fileId=2581431"
}
```

## Data sources

| Dataset | ID | Rows |
|---|---|---|
| UCC Filing Information | `wffy-3uut` | 2 577 084 |
| UCC Debtor Information | `8upq-58vz` | joined on `fileid` |
| Secured Party Information | `ap62-sav4` | joined on `fileid` |
| Business Entities in Colorado | `4ykn-tg5h` | joined on `entityname` |

Refreshed daily around 11:00 UTC. Coverage runs 1966 to yesterday, so a
7-14 day window is always populated.

## What the script handles

**Filtering to actual UCC-1.** The filing table mixes UCC financing statements
with IRS tax liens (262k rows), hospital liens (98k) and farm-product EFS
records (80k), and mixes initial filings with amendments, continuations and
terminations. Three predicates are required: `filingtype='ucc'`,
`documenttype='UCC financing statement'`, `transactiontype='Initial Filing'`.
Without them roughly half the result set is not a UCC-1.

**Batch joins.** Debtor and secured party are pulled with `fileid in(...)` in
chunks of 400 rather than one request per filing. At 500 leads a day that is
3 requests instead of 1500.

**Apostrophe escaping.** Real debtor names look like `Angelo's Taverna LLC`.
An unescaped quote breaks the SoQL query outright.

**Suffix repair.** Clerks routinely key the suffix into the last name field.
Live example: `lastname='Jr.'`, `firstname='Mauricio'`, `middlename='Garcia'`
is rebuilt as `Mauricio Garcia Jr.`

**Creditor name folding.** The portal stores `IRS`, `INTERNAL REVENUE SERVICE`
and `Internal Revenue Service` as three separate strings. `Samson` appears in
16 spellings. Any creditor filter has to compare folded keys, not raw text.

**Representative filers.** 15.5% of recent filings name CT Corporation,
Corporation Service Company or a similar agent as the secured party, hiding
the real funder. These are flagged rather than silently misclassified.

## Measured results

Sample of 497 leads from filings dated 29-31 July 2026:

| Metric | Value |
|---|---|
| Corporate debtors | 59.0% |
| Individual debtors (sole proprietors) | 41.0% |
| Leads with a resolved person name | 70.4% |
| Person resolved for LLC/Corp specifically | 49.8% |
| Secured party hidden behind an agent | 15.5% |

Owner resolution breakdown:

| Source | Share |
|---|---|
| `ucc_debtor` - debtor is a natural person | 41.0% |
| `registered_agent` - resolved via registry | 29.4% |
| `no_registry_match` - out-of-state entity | 15.3% |
| `agent_is_organisation` - law firm or agency | 12.3% |
| `commercial_agent` - national agent, not the owner | 2.0% |

The registered agent is a lead, not proof of ownership. National agents are
flagged separately so they never reach a dialer as a person.

## Notes

`APP_TOKEN` is a free Socrata application token. Without one the portal
throttles by IP; with one the limit is high enough for daily full-state runs.
