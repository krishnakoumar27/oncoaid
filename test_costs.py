import unittest
from decimal import Decimal
from costs import calculate_plan


class CostPlanTests(unittest.TestCase):
    def test_confirmed_and_potential_are_separate(self):
        plan = calculate_plan([{"name": "Surgery", "amount": "100000"}], [
            {"phase": "Surgery", "source": "Scheme A", "status": "confirmed", "amount": "40000"},
            {"phase": "Surgery", "source": "Trust B", "status": "potential", "amount": "20000"},
        ])
        self.assertEqual(plan["cash_conservative"], Decimal("60000"))
        self.assertEqual(plan["cash_if_potential_approved"], Decimal("40000"))

    def test_overlapping_allocations_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_plan([{"name": "Care", "amount": 100}], [
                {"phase": "Care", "source": "A", "status": "confirmed", "amount": 60},
                {"phase": "Care", "source": "B", "status": "potential", "amount": 50},
            ])

    def test_invalid_amount_is_rejected(self):
        for value in ("NaN", "-1", "0.001"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate_plan([{"name": "Care", "amount": value}], [])


if __name__ == "__main__":
    unittest.main()
