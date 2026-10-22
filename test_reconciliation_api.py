"""
Unit and integration tests for Phase 3: Payment Reconciliation.
Tests:
- Input validation (missing invoice, bad amount)
- Partial payment and remaining balance tracking
- Subsequent payment closing the balance (status -> 'paid')
- Automatic status update for linked registration record
- Overpayment detection (status -> 'overpaid', recon -> 'flagged')
"""

import json
import os
import unittest
from decimal import Decimal
from app import app, init_db, get_db

TEST_API_KEY = "test-secret-key-123"

class TestReconciliationApi(unittest.TestCase):

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

    def _create_test_registration_and_invoice(self, name="John Doe", fee="300.00", total_due="327.00"):
        """Helper to create a registration and linked finalized invoice."""
        headers = {"X-API-Key": TEST_API_KEY}
        reg_payload = {
            "customer_name": name,
            "customer_email": f"{name.lower().replace(' ', '')}@example.com",
            "programme_name": "AI Finance Workshop",
            "quantity": 1
        }
        res1 = self.client.post("/api/register", json=reg_payload, headers=headers)
        data1 = json.loads(res1.data)
        token = data1["draft_invoice"]["confirm_token"]
        reg_id = data1["registration_id"]

        # Finalize invoice
        res2 = self.client.post("/api/generate-invoice", json={"draft": False, "confirm_token": token}, headers=headers)
        data2 = json.loads(res2.data)
        inv_number = data2["invoice_number"]

        return reg_id, inv_number

    def test_payment_validation(self):
        headers = {"X-API-Key": TEST_API_KEY}

        # Missing invoice_number
        res1 = self.client.post("/api/payments/record", json={"amount_paid": "100.00"}, headers=headers)
        self.assertEqual(res1.status_code, 400)

        # Invalid amount (zero or negative)
        res2 = self.client.post("/api/payments/record", json={"invoice_number": "INV-DUMMY", "amount_paid": "0"}, headers=headers)
        self.assertEqual(res2.status_code, 400)

        # Non-existent invoice
        res3 = self.client.post("/api/payments/record", json={"invoice_number": "INV-NOT-FOUND-9999", "amount_paid": "100.00"}, headers=headers)
        self.assertEqual(res3.status_code, 404)

        print("PASS: Payment validation rejects missing fields, bad amounts, and invalid invoices")

    def test_partial_payment_tracking(self):
        headers = {"X-API-Key": TEST_API_KEY}
        reg_id, inv_number = self._create_test_registration_and_invoice("Alice Tan")

        # Total due is 327.00. Pay 150.00 (partial)
        pay_payload = {
            "invoice_number": inv_number,
            "amount_paid": "150.00",
            "payment_method": "paynow",
            "reference_number": "PAYNOW-REF-001"
        }
        res = self.client.post("/api/payments/record", json=pay_payload, headers=headers)
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)

        self.assertTrue(data["success"])
        recon = data["reconciliation"]
        self.assertEqual(recon["status"], "partial")
        self.assertEqual(recon["invoice_status"], "partially_paid")
        self.assertEqual(recon["total_paid"], "150.00")
        self.assertEqual(recon["remaining_balance"], "177.00")

        # Verify status endpoint reflects this
        status_res = self.client.get(f"/api/reconciliation/status/{inv_number}", headers=headers)
        self.assertEqual(status_res.status_code, 200)
        status_data = json.loads(status_res.data)
        self.assertEqual(status_data["invoice"]["status"], "partially_paid")
        self.assertEqual(len(status_data["payments"]), 1)

        print("PASS: Partial payment recorded and remaining balance tracked")

    def test_multi_payment_completion_and_registration_sync(self):
        headers = {"X-API-Key": TEST_API_KEY}
        reg_id, inv_number = self._create_test_registration_and_invoice("Bob Builder")

        # First payment: 200.00
        self.client.post("/api/payments/record", json={
            "invoice_number": inv_number,
            "amount_paid": "200.00"
        }, headers=headers)

        # Second payment: 127.00 (Total = 327.00, exact match)
        res2 = self.client.post("/api/payments/record", json={
            "invoice_number": inv_number,
            "amount_paid": "127.00"
        }, headers=headers)
        self.assertEqual(res2.status_code, 201)
        data2 = json.loads(res2.data)
        recon = data2["reconciliation"]

        self.assertEqual(recon["status"], "matched")
        self.assertEqual(recon["invoice_status"], "paid")
        self.assertEqual(recon["remaining_balance"], "0")

        # Verify DB: both invoice and registration should now be 'paid'
        with app.app_context():
            db = get_db()
            inv_row = db.execute("SELECT status FROM invoices WHERE invoice_number = ?", (inv_number,)).fetchone()
            self.assertEqual(inv_row["status"], "paid")

            reg_row = db.execute("SELECT status FROM registrations WHERE registration_id = ?", (reg_id,)).fetchone()
            self.assertEqual(reg_row["status"], "paid")

        print("PASS: Sequential payments aggregate cleanly, close invoice, and update registration")

    def test_overpayment_flagging(self):
        headers = {"X-API-Key": TEST_API_KEY}
        _, inv_number = self._create_test_registration_and_invoice("Charlie Chaplin")

        # Total due is 327.00. Pay 400.00 (overpayment)
        res = self.client.post("/api/payments/record", json={
            "invoice_number": inv_number,
            "amount_paid": "400.00",
            "payment_method": "bank_transfer"
        }, headers=headers)
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)
        recon = data["reconciliation"]

        self.assertEqual(recon["status"], "flagged")
        self.assertEqual(recon["invoice_status"], "overpaid")
        self.assertEqual(recon["total_paid"], "400.00")

        print("PASS: Overpayment flagged for manual review")


if __name__ == "__main__":
    unittest.main()