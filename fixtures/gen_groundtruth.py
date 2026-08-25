#!/usr/bin/env python3
"""
Ground truth canonico de los 5 fixtures.
Permite medir precision/recall a nivel de campo contra la salida del pipeline.
"""
import json, hashlib, os

OUT = "/home/claude/fixtures"
recs = []
INF = None  # bracket abierto por arriba

def add(**kw):
    recs.append(kw)

# ── DOC 1 ────────────────────────────────────────────────────────────
D1 = "01_federal_income_tax_rate_schedules_TY2026.pdf"
FS = ["single", "married_filing_jointly", "married_filing_separately", "head_of_household"]
B1 = [
    (0.10, [(0, 12250), (0, 24500), (0, 12250), (0, 17450)]),
    (0.12, [(12251, 49800), (24501, 99550), (12251, 49800), (17451, 66600)]),
    (0.22, [(49801, 106150), (99551, 212300), (49801, 106150), (66601, 106150)]),
    (0.24, [(106151, 202650), (212301, 405250), (106151, 202650), (106151, 202650)]),
    (0.32, [(202651, 257300), (405251, 514600), (202651, 257300), (202651, 257250)]),
    (0.35, [(257301, 643250), (514601, 771900), (257301, 385950), (257251, 643250)]),
    (0.37, [(643251, INF), (771901, INF), (385951, INF), (643251, INF)]),
]
for rate, cols in B1:
    for fs, (lo, hi) in zip(FS, cols):
        add(source_document=D1, source_page=1, table_id="table_1",
            record_type="ordinary_income_bracket", tax_year=2026, jurisdiction="US-FED",
            taxpayer_class="individual", filing_status=fs,
            lower_bound=lo, upper_bound=hi, rate=rate, currency="USD")

for rate, lo, hi in [(0.10, 0, 3250), (0.24, 3251, 11750), (0.35, 11751, 16050), (0.37, 16051, INF)]:
    add(source_document=D1, source_page=1, table_id="table_2",
        record_type="ordinary_income_bracket", tax_year=2026, jurisdiction="US-FED",
        taxpayer_class="estate_or_trust", filing_status=None,
        lower_bound=lo, upper_bound=hi, rate=rate, currency="USD")

# ── DOC 2 ────────────────────────────────────────────────────────────
D2 = "02_standard_deduction_schedule_TY2026.pdf"
for fs, amt, prior in [("single", 15400, 15000), ("married_filing_jointly", 30800, 30000),
                       ("married_filing_separately", 15400, 15000),
                       ("head_of_household", 23100, 22500),
                       ("qualifying_surviving_spouse", 30800, 30000)]:
    add(source_document=D2, source_page=1, table_id="section_1",
        record_type="standard_deduction", tax_year=2026, jurisdiction="US-FED",
        filing_status=fs, amount=amt, prior_year_amount=prior, currency="USD")

for cond, amt in [("unmarried", 2050), ("married_per_spouse", 1650)]:
    add(source_document=D2, source_page=1, table_id="section_2",
        record_type="additional_standard_deduction", tax_year=2026, jurisdiction="US-FED",
        condition=cond, amount=amt, currency="USD",
        note="allowed once for age 65+ and once for blindness")

add(source_document=D2, source_page=1, table_id="section_3",
    record_type="dependent_deduction_rule", tax_year=2026, jurisdiction="US-FED",
    floor_amount=1400, earned_income_addition=450, currency="USD",
    rule="max(1400, earned_income + 450), capped at basic standard deduction",
    extraction_note="expressed in prose, not tabular — expected to require a non-table path")

# ── DOC 3 ────────────────────────────────────────────────────────────
D3 = "03_state_local_sales_tax_rates_2026.pdf"
S3 = [("Alabama","AL",4.000,5.290),("Alaska","AK",None,1.821),("Arizona","AZ",5.600,2.777),
      ("Arkansas","AR",6.500,2.947),("California","CA",7.250,1.601),("Colorado","CO",2.900,4.913),
      ("Connecticut","CT",6.350,None),("Delaware","DE",None,None),
      ("District of Columbia","DC",6.000,None),("Florida","FL",6.000,1.002),
      ("Georgia","GA",4.000,3.384),("Hawaii","HI",4.000,0.500),("Idaho","ID",6.000,0.026),
      ("Illinois","IL",6.250,2.607),("Indiana","IN",7.000,None),("Iowa","IA",6.000,0.941),
      ("Kansas","KS",6.500,2.264),("Kentucky","KY",6.000,None),("Louisiana","LA",5.000,5.111),
      ("Maine","ME",5.500,None),("Maryland","MD",6.000,None),("Massachusetts","MA",6.250,None),
      ("Michigan","MI",6.000,None),("Minnesota","MN",6.875,1.156),("Mississippi","MS",7.000,0.062),
      ("Missouri","MO",4.225,4.166),("Montana","MT",None,None),("Nebraska","NE",5.500,1.483),
      ("Nevada","NV",6.850,1.386),("New Hampshire","NH",None,None),("New Jersey","NJ",6.625,-0.030),
      ("New Mexico","NM",4.875,2.760),("New York","NY",4.000,4.532),
      ("North Carolina","NC",4.750,2.246),("North Dakota","ND",5.000,2.041),("Ohio","OH",5.750,1.488),
      ("Oklahoma","OK",4.500,4.489),("Oregon","OR",None,None),("Pennsylvania","PA",6.000,0.341),
      ("Rhode Island","RI",7.000,None),("South Carolina","SC",6.000,1.499),
      ("South Dakota","SD",4.200,1.911),("Tennessee","TN",7.000,2.548),("Texas","TX",6.250,1.950),
      ("Utah","UT",6.100,1.153),("Vermont","VT",6.000,0.359),("Virginia","VA",5.300,0.471),
      ("Washington","WA",6.500,2.883),("West Virginia","WV",6.000,0.567),
      ("Wisconsin","WI",5.000,0.700),("Wyoming","WY",4.000,1.441)]
for i, (name, code, sr, lr) in enumerate(S3):
    comb = None if (sr is None and lr is None) else round((sr or 0) + (lr or 0), 3)
    add(source_document=D3, source_page=1 if i < 27 else 2, table_id="table_a",
        record_type="sales_tax_rate", effective_date="2026-01-01",
        jurisdiction=f"US-{code}", jurisdiction_name=name,
        state_rate_pct=sr, avg_local_rate_pct=lr, combined_rate_pct=comb,
        rate_unit="percent",
        imposes_state_sales_tax=sr is not None)

# ── DOC 4 ────────────────────────────────────────────────────────────
D4 = "04_employment_tax_rates_and_thresholds_2026.pdf"
for comp, ee, er, se in [("social_security_oasdi", 0.0620, 0.0620, 0.1240),
                         ("medicare_hi", 0.0145, 0.0145, 0.0290),
                         ("total", 0.0765, 0.0765, 0.1530)]:
    add(source_document=D4, source_page=1, table_id="table_1",
        record_type="employment_tax_rate", tax_year=2026, jurisdiction="US-FED",
        component=comp, employee_rate=ee, employer_rate=er, self_employed_rate=se)

for item, v25, v24 in [("social_security_wage_base", 181800, 176100),
                       ("medicare_wage_base", None, None),
                       ("futa_wage_base", 7000, 7000)]:
    add(source_document=D4, source_page=1, table_id="table_2",
        record_type="wage_base", tax_year=2026, jurisdiction="US-FED",
        item=item, amount=v25, prior_year_amount=v24, currency="USD",
        unlimited=(v25 is None))
add(source_document=D4, source_page=1, table_id="table_2",
    record_type="employment_tax_rate", tax_year=2026, jurisdiction="US-FED",
    component="futa_effective", employee_rate=None, employer_rate=0.0060, self_employed_rate=None)

for fs, th in [("married_filing_jointly", 250000), ("married_filing_separately", 125000),
               ("single", 200000), ("head_of_household", 200000),
               ("qualifying_surviving_spouse", 200000)]:
    add(source_document=D4, source_page=1, table_id="table_3",
        record_type="surtax_threshold", tax_year=2026, jurisdiction="US-FED",
        surtax="additional_medicare", rate=0.009, filing_status=fs,
        threshold=th, currency="USD", employer_match=False)

for per, n, allow in [("weekly", 52, 96.15), ("biweekly", 26, 192.30), ("semimonthly", 24, 208.33),
                      ("monthly", 12, 416.67), ("quarterly", 4, 1250.00), ("annually", 1, 5000.00)]:
    add(source_document=D4, source_page=1, table_id="table_4",
        record_type="withholding_allowance", tax_year=2026, jurisdiction="US-FED",
        payroll_period=per, periods_per_year=n, allowance=allow, currency="USD")

# ── DOC 5 ────────────────────────────────────────────────────────────
D5 = "05_capital_gains_preferential_rates_TY2025.pdf"
B5 = [
    (0.00, [(0, 48350), (0, 96700), (0, 48350), (0, 64750)]),
    (0.15, [(48351, 533400), (96701, 600050), (48351, 300000), (64751, 566700)]),
    (0.20, [(533401, INF), (600051, INF), (300001, INF), (566701, INF)]),
]
for rate, cols in B5:
    for fs, (lo, hi) in zip(FS, cols):
        add(source_document=D5, source_page=1, table_id="table_1",
            record_type="preferential_gain_bracket", tax_year=2025, lifecycle_status="superseded",
            superseded_effective="2026-01-01", jurisdiction="US-FED",
            filing_status=fs, lower_bound=lo, upper_bound=hi, rate=rate, currency="USD")

for cat, mx in [("unrecaptured_section_1250_gain", 0.25),
                ("collectibles_and_qsbs", 0.28)]:
    add(source_document=D5, source_page=1, table_id="table_2",
        record_type="special_gain_rate", tax_year=2025, lifecycle_status="superseded",
            superseded_effective="2026-01-01", jurisdiction="US-FED",
        category=cat, max_rate=mx)
add(source_document=D5, source_page=1, table_id="table_2",
    record_type="special_gain_rate", tax_year=2025, lifecycle_status="superseded",
            superseded_effective="2026-01-01", jurisdiction="US-FED",
    category="short_term_capital_gain", max_rate=None,
    note="taxed at ordinary rates — see document 01")

for fs, th in [("single", 200000), ("head_of_household", 200000),
               ("married_filing_jointly", 250000), ("married_filing_separately", 125000)]:
    add(source_document=D5, source_page=1, table_id="footnote",
        record_type="surtax_threshold", tax_year=2025, lifecycle_status="superseded",
            superseded_effective="2026-01-01", jurisdiction="US-FED",
        surtax="net_investment_income", rate=0.038, filing_status=fs,
        threshold=th, currency="USD",
        extraction_note="stated only in a footnote, not in any table")

# ── salida ───────────────────────────────────────────────────────────
def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

docs = sorted(f for f in os.listdir(OUT) if f.endswith(".pdf"))
by_doc, by_type = {}, {}
for r in recs:
    by_doc[r["source_document"]] = by_doc.get(r["source_document"], 0) + 1
    by_type[r["record_type"]] = by_type.get(r["record_type"], 0) + 1

manifest = {
    "schema_version": "1.0",
    "description": "Ground truth for the tax-table extraction fixture set. "
                   "Compare pipeline output field-by-field against expected_records.",
    "data_disclaimer": "Synthetic values with realistic structure. Not authoritative tax data.",
    "deliberate_traps": [
        "doc 02 carries bulletin id TB-2025-14 but applies to TY2026 — tax_year must be read "
        "from the effective-date sentence, never inferred from the document id or filename",
        "doc 02 and doc 04 each hold two tax years in one row (current + prior column); a naive "
        "extractor keeps only one and silently loses half the records",
        "doc 03 uses a long dash for 'no tax imposed' — null, not zero",
        "doc 05 has no text layer at all and is superseded; it must be ingested via OCR and must "
        "not surface in tax_year=2026 queries",
    ],
    "conventions": {
        "rate": "decimal fraction (0.22 = 22%) except *_pct fields, which are percentages",
        "tax_year": "documents 01-04 are TY/CY/FY2026; document 05 is TY2025 and superseded",
        "lifecycle_status": "absent means active; 'superseded' means the record must not be "
                            "returned for tax_year 2026 queries",
        "upper_bound": "null means the bracket is open-ended",
        "state_rate_pct": "null means the jurisdiction imposes no tax of that type (distinct from 0)",
        "amounts": "integers in the stated currency unless the source prints cents",
    },
    "documents": [{
        "file": f,
        "sha256": sha(os.path.join(OUT, f)),
        "bytes": os.path.getsize(os.path.join(OUT, f)),
        "expected_record_count": by_doc.get(f, 0),
    } for f in docs],
    "totals": {"records": len(recs), "by_record_type": dict(sorted(by_type.items()))},
    "expected_records": recs,
}

with open(f"{OUT}/ground_truth.json", "w") as fh:
    json.dump(manifest, fh, indent=2, ensure_ascii=False)

print(f"registros esperados: {len(recs)}")
for f in docs:
    print(f"  {by_doc.get(f,0):>4}  {f}")
print("\npor tipo:")
for k, v in sorted(by_type.items(), key=lambda x: -x[1]):
    print(f"  {v:>4}  {k}")
