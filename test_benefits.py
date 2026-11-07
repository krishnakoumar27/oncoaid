import unittest
from decimal import Decimal
from benefits import project_benefit, patient_projection, assessment_context


class SchemeBenefitTests(unittest.TestCase):
    def test_package_limit_reduces_cost_and_caps_to_expenses(self):
        rule = {'coverage_kind':'package_limit','coverage_cap':500000}
        result = project_benefit('800000', rule)
        self.assertEqual(result['possible_cover'], Decimal('500000'))
        self.assertEqual(result['patient_share'], Decimal('300000'))
        self.assertEqual(result['percent'], Decimal('62.50'))
        self.assertTrue(result['upper_bound'])
        self.assertEqual(project_benefit('200000',rule)['possible_cover'], Decimal('200000'))

    def test_known_balance_and_covered_expenses_reduce_available_benefit(self):
        rule = {'coverage_kind':'package_limit','coverage_cap':500000}
        result = project_benefit('800000',rule,eligible_expenses='450000',remaining_balance='200000')
        self.assertEqual(result['patient_share'], Decimal('600000'))
        self.assertEqual(project_benefit('800000',rule,remaining_balance='0')['possible_cover'], 0)

    def test_grant_ceiling_is_distinct_from_actual_case_amount(self):
        rule = {'coverage_kind':'grant_limit','coverage_cap':1500000}
        maximum = project_benefit('800000',rule)
        self.assertEqual(maximum['patient_share'], 0)
        self.assertTrue(maximum['upper_bound'])
        actual = project_benefit('800000',rule,case_amount='250000')
        self.assertEqual(actual['patient_share'], Decimal('550000'))
        self.assertFalse(actual['upper_bound'])

    def test_unknown_is_not_zero_and_percentage_remains_supported(self):
        self.assertIsNone(project_benefit('800000',{}))
        self.assertEqual(project_benefit('800000',{},case_amount='30000')['possible_cover'], Decimal('30000'))
        self.assertEqual(project_benefit('800000',{'coverage_rate':50,'coverage_cap':250000})['patient_share'], Decimal('550000'))

    def test_invalid_inputs_cannot_create_negative_cash_or_false_funding(self):
        for kwargs in ({'eligible_expenses':'900000'}, {'remaining_balance':'-1'}, {'case_amount':'NaN'}, {'case_amount':'600000'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                project_benefit('800000',{'coverage_kind':'package_limit','coverage_cap':500000},**kwargs)

    def test_patient_assessments_are_invalidated_by_context_or_source_changes(self):
        facts={'state':'Tamil Nadu','diagnosis':'Documented diagnosis'}
        rule={'id':1,'revision':2,'details':{'coverage_kind':'package_limit','coverage_cap':500000}}
        patient={'funding_assessments':{'1':{'scheme_revision':2,'total':'800000.00','context':assessment_context(facts),
                                          'eligible_expenses':'700000','remaining_balance':'100000','case_amount':None,'evidence':'Benefit balance reference'}}}
        self.assertEqual(patient_projection(patient,rule,'800000',facts)['possible_cover'], Decimal('100000'))
        self.assertTrue(patient_projection(patient,rule,'800000',facts)['case_inputs_verified'])
        changed={**facts,'diagnosis':'Changed diagnosis'}
        self.assertFalse(patient_projection(patient,rule,'800000',changed)['case_inputs_verified'])
        rule['revision']=3
        self.assertFalse(patient_projection(patient,rule,'800000',facts)['case_inputs_verified'])


if __name__=='__main__':
    unittest.main()
