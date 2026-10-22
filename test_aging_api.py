"""
Unit tests for Phase 4: Invoice Aging & Escalation Engine.
Tests:
- Future due date -> 'current' bucket
- Overdue 1-30 days -> '1_30_days' bucket with gentle reminder flag
- Overdue 31-60 days -> '31_60_days' bucket with formal demand flag
- Overdue 60+ days -> '60_plus_days' bucket with admin hold flag
- Overpaid invoices routed to exceptions queue
- Autonomous generation of personalized escalation notices
"""

import json
import os
import unittest
from datetime import date, timedelta
from app import app, init_db, get_db

TEST_API_KEY = "test-secret-key-123"


class TestAgingApi(unittest.TestCase):

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

    def _insert_manual_invoice(self, inv_num, name, due_date_str, total_due="300.00", status="unpaid"):
        with app.app_context():
            db = get_db()
            db.execute(
                """
                INSERT INTO invoices (
                    invoice_number, customer_name, customer_email, description,
                    quantity, unit_price, subtotal, total_due, due_date, issue_date, status, created_at
                ) VALUES (?, ?, ?, 'Workshop', 1, ?, ?, ?, ?, '2026-01-01', ?, '2026-01-01 10:00:00')
                """,
                (inv_num, name, f"{name.lower().replace(' ', '')}@example.com", total_due, total_due, total_due, due_date_str, status)
            )
            db.commit()

    def test_aging_buckets(self):
        headers = {"X-API-Key": TEST_API_KEY}
        today = date.today()

        # Current (due in 5 days)
        self._insert_manual_invoice("INV-CURRENT", "Client A", (today + timedelta(days=5)).isoformat())

        # 1-30 days overdue (due 10 days ago)
        self._insert_manual_invoice("INV-1-30", "Client B", (today - timedelta(days=10)).isoformat())

        # 31-60 days overdue (due 45 days ago)
        self._insert_manual_invoice("INV-31-60", "Client C", (today - timedelta(days=45)).isoformat())

        # 60+ days overdue (due 80 days ago)
        self._insert_manual_invoice("INV-60-PLUS", "Client D", (today - timedelta(days=80)).isoformat())

        # Overpaid exception
        self._insert_manual_invoice("INV-OVERPAID", "Client E", (today - timedelta(days=5)).isoformat(), status="overpaid")

        res = self.client.get("/api/reconciliation/aging-report", headers=headers)
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)

        self.assertTrue(data["success"])
        summary = data["summary"]
        self.assertEqual(summary["current_count"], 1)
        self.assertEqual(summary["1_30_days_count"], 1)
        self.assertEqual(summary["31_60_days_count"], 1)
        self.assertEqual(summary["60_plus_days_count"], 1)
        self.assertEqual(summary["overpaid_count"], 1)

        buckets = data["aging_buckets"]
        self.assertEqual(buckets["1_30_days"][0]["escalation_level"], "gentle_reminder")
        self.assertEqual(buckets["31_60_days"][0]["escalation_level"], "formal_demand")
        self.assertEqual(buckets["60_plus_days"][0]["escalation_level"], "admin_hold")

        print("PASS: Invoices properly classified into aging buckets and escalation tiers")

    def test_escalation_notice_generation(self):
        headers = {"X-API-Key": TEST_API_KEY}
        today = date.today()

        # Insert 1 overdue invoice (20 days overdue)