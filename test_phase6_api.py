import json
import os
import unittest
from decimal import Decimal
from app import app, init_db, get_db

TEST_API_KEY = "test-secret-key-123"

class TestPhase6MultiCurrency(unittest.TestCase):

    def setUp(self):
        os.environ["FINANCE_API_KEY"] = TEST_API_KEY
        os.environ["TESTING"] = "true"
        app.config["TESTING"] = True
        self.client = app.test_client()
        with app.app_context():
            init_db()
            db = get_db()
            db.execute("DELETE FROM payments")
            db.execute("DELETE FROM registrations")
            db.execute("DELETE FROM invoices")
            db.commit()

    def test_fx_conversion_and_settlement(self):
        headers = {"X-API-Key": TEST_API_KEY}

        # Seed 1 invoice for SGD 269.00 (200 USD @ 1.345 = 269.00 SGD)
        with app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO invoices (
                    invoice_number, customer_name, customer_email, description,
                    quantity, unit_price, subtotal, total_due, due_date, issue_date, status, created_at
                ) VALUES 
                ('INV-FX-01', 'Global Corp', 'finance@global.com', 'Training', 1, '269.00', '269.00', '269.00', '2026-11-20', '2026-10-22', 'unpaid', '2026-10-22 10:00:00')
                """
            )
            db.commit()

        # Step 1: Query FX Rates
        rate_res = self.client.get("/api/fx-rates")
        self.assertEqual(rate_res.status_code, 200)
        rate_data = json.loads(rate_res.data)
        self.assertTrue(rate_data["success"])
        self.assertEqual(rate_data["rates"]["USD"], "1.3450")

        # Step 2: Pay in USD
        pay_payload = {
            "invoice_number": "INV-FX-01",
            "amount_paid": "200.00",
            "currency": "USD",
            "payment_method": "bank_transfer",
            "reference_number": "WIRE-US-9981"
        }
        pay_res = self.client.post("/api/payments/multi-currency-record", json=pay_payload, headers=headers)
        self.assertEqual(pay_res.status_code, 201)
        pay_data = json.loads(pay_res.data)

        self.assertTrue(pay_data["success"])
        self.assertEqual(pay_data["fx_conversion"]["sgd_credited"], "269.00")
        self.assertEqual(pay_data["reconciliation"]["invoice_status"], "paid")
        self.assertEqual(pay_data["reconciliation"]["remaining_balance_sgd"], "0.00")

        print("PASS: FX payment converted accurately to SGD and reconciled to paid status")

if __name__ == "__main__":
    unittest.main()