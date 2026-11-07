"""Deterministic stand-alone scheme projections. Unknown benefits remain unknown."""
import hashlib
import json
from decimal import Decimal, ROUND_DOWN

from costs import amount


def coverage_kind(details):
    return details.get('coverage_kind') or ('percentage' if details.get('coverage_rate') is not None
                                           else 'grant_limit' if details.get('coverage_cap') is not None else 'case_by_case')


def assessment_context(facts):
    keys = ('state', 'hospital', 'diagnosis', 'stage', 'planned_treatment', 'treatment_units', 'income', 'insurance')
    return hashlib.sha256(json.dumps({k: str(facts[k]) if facts.get(k) is not None else '' for k in keys}, sort_keys=True).encode()).hexdigest()


def project_benefit(total, details, eligible_expenses=None, remaining_balance=None, case_amount=None):
    """A ceiling is an upper-bound scenario, never a prediction of an award."""
    total = amount(total)
    if total <= 0:
        return None
    eligible = total if eligible_expenses is None else amount(eligible_expenses)
    if eligible > total:
        raise ValueError('Eligible expenses exceed the treatment estimate.')
    kind = coverage_kind(details)
    notes = []
    if eligible_expenses is None:
        notes.append('Assumes all estimated expenses qualify; the hospital must verify the covered package.')
    cap = amount(details['coverage_cap']) if details.get('coverage_cap') is not None else None
    if kind == 'percentage':
        rate = amount(details.get('coverage_rate'))
        if not 0 < rate <= 100:
            raise ValueError('Coverage percentage must be above zero and at most 100.')
        cover = eligible * rate / 100
        if cap is not None:
            cover = min(cover, cap)
    elif kind in {'package_limit', 'grant_limit'} and cap is not None:
        cover = min(eligible, cap)
    elif case_amount is not None:
        cover = min(eligible, amount(case_amount))
    else:
        return None
    if remaining_balance is not None:
        cover = min(cover, amount(remaining_balance))
    elif kind == 'package_limit':
        notes.append('Assumes the full family-year benefit remains available; previous claims may reduce it.')
    if case_amount is not None:
        approved = amount(case_amount)
        if cap is not None and approved > cap:
            raise ValueError('Case-specific benefit exceeds the published ceiling.')
        cover = min(cover, approved)
        notes.append('Uses the case-specific amount checked by the worker.')
    elif kind == 'grant_limit':
        notes.append('This is the maximum grant scenario, not an expected or sanctioned grant. Actual awards are case-by-case.')
    cover = min(total, cover).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    return {'total': total, 'possible_cover': cover, 'percent': (cover/total*100).quantize(Decimal('0.01'), rounding=ROUND_DOWN),
            'patient_share': total-cover, 'coverage_kind': kind, 'coverage_cap': cap,
            'upper_bound': kind in {'package_limit', 'grant_limit'} and case_amount is None,
            'assumptions': notes, 'source': details.get('coverage_source') or details.get('source', ''),
            'checked': details.get('coverage_checked'), 'notes': details.get('coverage_notes', ''),
            'subject_to_approval': True}


def patient_projection(patient, scheme, total, facts):
    saved = (patient.get('funding_assessments') or {}).get(str(scheme['id'])) or {}
    valid = (saved.get('scheme_revision') == scheme['revision'] and saved.get('total') == format(amount(total), '.2f')
             and saved.get('context') == assessment_context(facts))
    inputs = {k: saved.get(k) for k in ('eligible_expenses', 'remaining_balance', 'case_amount')} if valid else {}
    projection = project_benefit(total, scheme['details'], **inputs)
    if projection:
        projection['case_inputs_verified'] = valid
        projection['case_evidence'] = saved.get('evidence', '') if valid else ''
    return projection
