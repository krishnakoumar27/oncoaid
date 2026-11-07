"""Sourced starting points for screening, with no inferred grant promises."""
SOURCES = {
    "CMCHIS": {
        "source": "https://www.cmchistn.com/eligibility",
        "guidance": "Tamil Nadu enrollment screening generally requires annual family income below ₹1,20,000. Check income certificate, Tamil Nadu family card, identity proof, enrollment, hospital empanelment and the exact covered package. Special-category exceptions require scheme-desk review. The annual family benefit ceiling is not a promised payment. Source checked 5 November 2026.",
        "requirements": ["Income certificate", "Tamil Nadu family card", "Identity proof", "Hospital treatment estimate"],
    },
    "PM-JAY": {
        "source": "https://nha.gov.in/img/resources/Operation%20Manual%20for%20AB%20PM-JAY.pdf",
        "guidance": "Check official beneficiary enrollment, empanelled hospital, the prescribed treatment package, preauthorization and remaining family benefit. Low income or cancer diagnosis alone does not establish enrollment or a grant amount. Source checked 5 November 2026.",
        "requirements": ["Beneficiary verification", "Identity proof", "Hospital treatment estimate"],
    },
    "Health Minister's Cancer Patient Fund (HMCPF) under RAN": {
        "source": "https://www.mohfw.gov.in/sites/default/files/2563214569875214.pdf",
        "guidance": "Cancer funding pathway for patients below the poverty line at designated regional cancer centres. Check current hospital eligibility, government/PSU employment exclusions, other assistance and whether care is already provided free. Ask the hospital for the authorized case-specific award and current RAN rules. Source checked 5 November 2026.",
        "requirements": ["Income certificate", "Ration card", "Hospital-certified application", "Treatment estimate"],
    },
}


BENEFITS = {
    'CMCHIS': {
        'coverage_kind': 'package_limit', 'coverage_cap': 500000,
        'coverage_source': 'https://cmchistn.com/enrollment/treatment-covered',
        'coverage_checked': '2026-11-06',
        'coverage_notes': 'Up to ₹5 lakh per family per year for listed cashless packages at network hospitals. The covered package, e-card and unused family benefit must be verified.',
    },
    'PM-JAY': {
        'coverage_kind': 'package_limit', 'coverage_cap': 500000,
        'coverage_source': 'https://www.rmlh.mohfw.gov.in/index1.aspx?langid=1&lev=2&lid=3619&lsid=2566',
        'coverage_checked': '2026-11-06',
        'coverage_notes': 'Up to ₹5 lakh per eligible family per year for covered hospitalization packages. Enrollment, hospital empanelment, preauthorization and remaining family balance must be checked.',
    },
    "Health Minister's Cancer Patient Fund (HMCPF) under RAN": {
        'coverage_kind': 'grant_limit', 'coverage_cap': 1500000,
        'coverage_source': 'https://www.pib.gov.in/PressReleasePage.aspx?PRID=2154159&lang=1&reg=3',
        'coverage_checked': '2026-11-06',
        'coverage_notes': 'One-time assistance has a ceiling of ₹15 lakh. This is not a typical award or a guaranteed discount. Poverty/NFSA pathway, eligible government cancer facility and case sanction must be checked. Other assistance cannot be double counted.',
    },
}


def seed_sources(db, audit):
    from psycopg.types.json import Jsonb
    for name, source in SOURCES.items():
        row = db.execute("SELECT * FROM schemes WHERE name=%s FOR UPDATE", (name,)).fetchone()
        if row and not row["details"].get("source") and not row["details"].get("guidance"):
            details = {**row["details"], **source, "published": True}
            db.execute("UPDATE schemes SET details=%s,revision=revision+1 WHERE id=%s",
                       (Jsonb(details),row["id"]))
            audit(db,None,"sourced_screening_entry_added",{"scheme":row["id"],"source":source["source"]})
        # Upgrade the original sourced entries once, preserving custom financial rules.
        row = db.execute('SELECT * FROM schemes WHERE name=%s FOR UPDATE', (name,)).fetchone()
        if (row and not row['details'].get('benefit_seed_version')
                and row['details'].get('source') == source['source']
                and row['details'].get('coverage_rate') is None and row['details'].get('coverage_cap') is None):
            details = {**row['details'], **BENEFITS[name], 'benefit_seed_version': 1}
            if name.startswith("Health Minister's") and row['details'].get('guidance') == source['guidance']:
                details['guidance'] = ('Assistance is assessed case-by-case for poor cancer patients through eligible government cancer facilities. '
                                       'Check the state poverty threshold or integrated NFSA pathway, hospital eligibility, other assistance, '
                                       'the prescribed form and official sanction. The published ₹15 lakh ceiling does not establish the actual award. '
                                       'Current Ministry update checked 6 November 2026.')
                details['source'] = BENEFITS[name]['coverage_source']
            db.execute('UPDATE schemes SET details=%s,revision=revision+1 WHERE id=%s', (Jsonb(details), row['id']))
            audit(db, None, 'sourced_benefit_rule_added', {'scheme': row['id'], 'benefit': BENEFITS[name]})
