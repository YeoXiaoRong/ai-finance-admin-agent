"""
Unit tests for Phase 2: Registration & Invoice Automation.
Uses standard library unittest (no external pytest needed).
Tests:
- Listing available programmes
- Successful registration with automated draft creation
- Input validations (missing name, bad email, unknown programme)
- Duplicate registration rejection
- Requiring human approval via confirmation_token
"""

"""
Unit tests for Phase 2: Registration & Invoice Automation.
Uses standard library unittest.
"""

import json
import os
import unittest
from app import app, init_db, get_db

TEST_API_KEY = "test-secret-key-123"

class TestRegistrationApi(unittest.TestCase):
    
    def setUp(self):
        os.environ["FINANCE_API_KEY"] = TEST_API_KEY
        os.environ["TESTING"] = "true"
        app.config["TESTING"] = True
        self.client = app.test_client()
        with app.app_context():
            init_db()
            # Clean registrations before each test run
            db = get_db()
            db.execute("DELETE FROM registrations")
            db.commit()

    def test_list_programmes(self):
        res = self.client.get("/api/programmes")
        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertTrue(data["success"])
        self.assertGreaterEqual(len(data["programmes"]), 3)
        names = [p["name"] for p in data["programmes"]]
        self.assertIn("AI Finance Workshop", names)
        print("PASS: List programmes returns standard catalog")

    def test_successful_registration_and_draft_creation(self):
        payload = {
            "customer_name": "Alice Tan",
            "customer_email": "alice@example.com",
            "programme_name": "AI Finance Workshop",
            "quantity": 1
        }
        headers = {"X-API-Key": TEST_API_KEY}
        res = self.client.post("/api/register", json=payload, headers=headers)
        if res.status_code != 201:
            print("ERROR BODY:", res.get_data(as_text=True))
        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)

        
        self.assertTrue(data["success"])
        self.assertIn("registration_id", data)
        self.assertTrue(data["requires_approval"])
        self.assertTrue("draft_token" in data["draft_invoice"] or "confirm_token" in data["draft_invoice"])
        self.assertEqual(data["draft_invoice"]["subtotal"], "300.00")
        self.assertEqual(data["draft_invoice"]["total_due"], "327.00")  # 300 + 9% tax (27)
        print("PASS: Registration succeeds and creates draft with approval token")

    def test_registration_validation_missing_fields(self):
        headers = {"X-API-Key": TEST_API_KEY}
        
        # Missing email
        res1 = self.client.post("/api/register", json={"customer_name": "Bob"}, headers=headers)
        self.assertEqual(res1.status_code, 400)
        
        # Invalid email
        res2 = self.client.post("/api/register", json={
            "customer_name": "Bob",
            "customer_email": "invalid-email",
            "programme_name": "AI Finance Workshop"
        }, headers=headers)
        self.assertEqual(res2.status_code, 400)

        # Unknown programme
        res3 = self.client.post("/api/register", json={
            "customer_name": "Bob",
            "customer_email": "bob@example.com",
            "programme_name": "Underwater Basket Weaving"
        }, headers=headers)
        self.assertEqual(res3.status_code, 400)
        print("PASS: Registration validates required fields and catalog existence")

    def test_duplicate_registration_rejected(self):
        headers = {"X-API-Key": TEST_API_KEY}
        payload = {
            "customer_name": "Charlie",
            "customer_email": "charlie@example.com",
            "programme_name": "AI Finance Workshop"
        }
        # First registration
        res1 = self.client.post("/api/register", json=payload, headers=headers)
        if res1.status_code != 201:
            print("ERROR BODY RES1:", res1.get_data(as_text=True))
        self.assertEqual(res1.status_code, 201)
        
        # Duplicate registration
        res2 = self.client.post("/api/register", json=payload, headers=headers)
        self.assertEqual(res2.status_code, 400)
        self.assertIn("already registered", json.loads(res2.data)["error"])
        print("PASS: Duplicate registration blocked with 400 error")

    def test_registration_to_finalized_invoice_flow(self):
        headers = {"X-API-Key": TEST_API_KEY}
        
        # 1. Register
        payload = {
            "customer_name": "Diana Prince",
            "customer_email": "diana@example.com",
            "programme_name": "Executive Leadership Masterclass",
            "quantity": 1
        }
        res1 = self.client.post("/api/register", json=payload, headers=headers)
        self.assertEqual(res1.status_code, 201)
        data1 = json.loads(res1.data)
        token = data1["draft_invoice"]["confirm_token"]
        reg_id = data1["registration_id"]

        # 2. Human approval -> finalize invoice via /api/generate-invoice
        confirm_payload = {
            "draft": False,
            "confirm_token": token
        }
        res2 = self.client.post("/api/generate-invoice", json=confirm_payload, headers=headers)
        self.assertEqual(res2.status_code, 201)
        data2 = json.loads(res2.data)
        self.assertTrue(data2["success"])
        inv_number = data2["invoice_number"]

        # 3. Verify registration record status in database is now 'invoiced'
        with app.app_context():
            db = get_db()
            row = db.execute("SELECT status, invoice_number FROM registrations WHERE registration_id = ?", (reg_id,)).fetchone()
            self.assertEqual(row["status"], "invoiced")
            self.assertEqual(row["invoice_number"], inv_number)
            
        print("PASS: Registration -> Human Approval -> Finalized Invoice & DB link")

if __name__ == "__main__":
    unittest.main()