"""
Tests for the AI Finance Admin Agent — discount/tax calculation and validation.

Run with:
    python test_invoice_calc.py
(from the ai-finance-admin directory)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from decimal import Decimal
from app import compute_totals, validate_invoice_input, app, init_db

TWO = Decimal("0.01")

# -----------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------

def run(label, test_fn):
    try:
        test_fn()
        print(f"  PASS  {label}")
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        return False
    return True


all_passed = True

def case(label, fn):
    global all_passed
    if not run(label, fn):
        all_passed = False


# -----------------------------------------------------------------------
# 1. Core scenario: qty 3, unit 100, fixed discount 55, tax 9%
#    Expected: subtotal 300.00, discount 55.00, tax 22.05, total 267.05
# -----------------------------------------------------------------------

def test_core_scenario():
    r = compute_totals(
        quantity=3,
        unit_price=Decimal("100"),
        discount_type="fixed",
        discount_value=Decimal("55"),
        tax_rate=Decimal("9"),
    )
    assert r["subtotal"]        == Decimal("300.00"), f"subtotal {r['subtotal']}"
    assert r["discount_amount"] == Decimal("55.00"),  f"discount {r['discount_amount']}"
    assert r["taxable_amount"]  == Decimal("245.00"), f"taxable {r['taxable_amount']}"
    assert r["tax_amount"]      == Decimal("22.05"),  f"tax {r['tax_amount']}"
    assert r["total_due"]       == Decimal("267.05"), f"total {r['total_due']}"

case("Core scenario (qty=3, price=100, fixed discount=55, tax=9%)", test_core_scenario)


# -----------------------------------------------------------------------
# 2. Zero tax
# -----------------------------------------------------------------------

def test_zero_tax():
    r = compute_totals(3, Decimal("100"), "fixed", Decimal("55"), Decimal("0"))
    assert r["tax_amount"] == Decimal("0.00"), f"tax {r['tax_amount']}"
    assert r["total_due"]  == Decimal("245.00"), f"total {r['total_due']}"

case("Zero tax -> total = taxable amount", test_zero_tax)


# -----------------------------------------------------------------------
# 3. Percentage discount 10%
# -----------------------------------------------------------------------

def test_percentage_discount():
    r = compute_totals(3, Decimal("100"), "percentage", Decimal("10"), Decimal("0"))
    assert r["subtotal"]        == Decimal("300.00"), f"subtotal {r['subtotal']}"
    assert r["discount_amount"] == Decimal("30.00"),  f"discount {r['discount_amount']}"
    assert r["taxable_amount"]  == Decimal("270.00"), f"taxable {r['taxable_amount']}"
    assert r["total_due"]       == Decimal("270.00"), f"total {r['total_due']}"

case("Percentage discount 10% (no tax)", test_percentage_discount)


# -----------------------------------------------------------------------
# 4. No discount
# -----------------------------------------------------------------------

def test_no_discount():
    r = compute_totals(2, Decimal("50.00"), "none", Decimal("0"), Decimal("7"))
    assert r["subtotal"]        == Decimal("100.00"), f"subtotal {r['subtotal']}"
    assert r["discount_amount"] == Decimal("0.00"),   f"discount {r['discount_amount']}"
    assert r["tax_amount"]      == Decimal("7.00"),   f"tax {r['tax_amount']}"
    assert r["total_due"]       == Decimal("107.00"), f"total {r['total_due']}"

case("No discount, 7% tax", test_no_discount)


# -----------------------------------------------------------------------
# 5. Rounding: ensure ROUND_HALF_UP
#    subtotal=10, 33.333...% tax  → 3.33
# -----------------------------------------------------------------------

def test_rounding():
    r = compute_totals(1, Decimal("10"), "none", Decimal("0"), Decimal("33.3333"))
    assert r["tax_amount"] == Decimal("3.33"), f"tax {r['tax_amount']}"

case("ROUND_HALF_UP rounding of tax", test_rounding)


# -----------------------------------------------------------------------
# 6. Validation: qty=0 rejected
# -----------------------------------------------------------------------

def test_invalid_qty_zero():
    errors = validate_invoice_input({
        "customer_name": "Test", "customer_email": "a@b.com",
        "description": "svc", "quantity": "0", "unit_price": "100",
        "due_date": "2099-01-01",
        "discount_type": "none", "discount_value": "0", "tax_rate": "0",
    })
    assert any("Quantity" in e for e in errors), f"Expected qty error, got: {errors}"

case("Validation: quantity=0 rejected", test_invalid_qty_zero)


# -----------------------------------------------------------------------
# 7. Validation: negative unit price rejected
# -----------------------------------------------------------------------

def test_invalid_negative_price():
    errors = validate_invoice_input({
        "customer_name": "Test", "customer_email": "a@b.com",
        "description": "svc", "quantity": "1", "unit_price": "-5",
        "due_date": "2099-01-01",
        "discount_type": "none", "discount_value": "0", "tax_rate": "0",
    })
    assert any("price" in e.lower() for e in errors), f"Expected price error, got: {errors}"

case("Validation: negative unit price rejected", test_invalid_negative_price)


# -----------------------------------------------------------------------
# 8. Validation: fixed discount > subtotal rejected
# -----------------------------------------------------------------------

def test_invalid_discount_exceeds_subtotal():
    errors = validate_invoice_input({
        "customer_name": "Test", "customer_email": "a@b.com",
        "description": "svc", "quantity": "3", "unit_price": "100",
        "due_date": "2099-01-01",
        "discount_type": "fixed", "discount_value": "400",  # 400 > 300
        "tax_rate": "0",
    })
    assert any("discount" in e.lower() or "subtotal" in e.lower() for e in errors), \
        f"Expected discount error, got: {errors}"

case("Validation: fixed discount > subtotal rejected", test_invalid_discount_exceeds_subtotal)


# -----------------------------------------------------------------------
# 9. Validation: percentage discount > 100 rejected
# -----------------------------------------------------------------------

def test_invalid_pct_discount_over_100():
    errors = validate_invoice_input({
        "customer_name": "Test", "customer_email": "a@b.com",
        "description": "svc", "quantity": "1", "unit_price": "100",
        "due_date": "2099-01-01",
        "discount_type": "percentage", "discount_value": "110",
        "tax_rate": "0",
    })
    assert any("100" in e or "percent" in e.lower() or "exceed" in e.lower() for e in errors), \
        f"Expected pct error, got: {errors}"

case("Validation: percentage discount > 100% rejected", test_invalid_pct_discount_over_100)


# -----------------------------------------------------------------------
# 10. Validation: negative discount rejected
# -----------------------------------------------------------------------

def test_invalid_negative_discount():
    errors = validate_invoice_input({
        "customer_name": "Test", "customer_email": "a@b.com",
        "description": "svc", "quantity": "1", "unit_price": "100",
        "due_date": "2099-01-01",
        "discount_type": "fixed", "discount_value": "-10",
        "tax_rate": "0",
    })
    assert any("discount" in e.lower() and "negative" in e.lower() for e in errors), \
        f"Expected negative discount error, got: {errors}"

case("Validation: negative discount rejected", test_invalid_negative_discount)


# -----------------------------------------------------------------------
# 11. Validation: negative tax rate rejected
# -----------------------------------------------------------------------

def test_invalid_negative_tax():
    errors = validate_invoice_input({
        "customer_name": "Test", "customer_email": "a@b.com",
        "description": "svc", "quantity": "1", "unit_price": "100",
        "due_date": "2099-01-01",
        "discount_type": "none", "discount_value": "0",
        "tax_rate": "-1",
    })
    assert any("tax" in e.lower() and "negative" in e.lower() for e in errors), \
        f"Expected tax error, got: {errors}"

case("Validation: negative tax rate rejected", test_invalid_negative_tax)


# -----------------------------------------------------------------------
# 12. Valid input passes validation (no errors)
# -----------------------------------------------------------------------

def test_valid_input_no_errors():
    errors = validate_invoice_input({
        "customer_name": "Acme", "customer_email": "a@b.com",
        "description": "svc", "quantity": "3", "unit_price": "100",
        "due_date": "2099-01-01",
        "discount_type": "fixed", "discount_value": "55",
        "tax_rate": "9",
    })
    assert errors == [], f"Expected no errors, got: {errors}"

case("Validation: valid input passes with no errors", test_valid_input_no_errors)


# -----------------------------------------------------------------------
# 13. Flask API integration test via test client
# -----------------------------------------------------------------------

def test_api_create_invoice():
    with app.test_client() as client:
        with app.app_context():
            init_db()
        payload = {
            "customer_name": "Test Co",
            "customer_email": "test@example.com",
            "description": "Test Service",
            "quantity": "3",
            "unit_price": "100",
            "discount_type": "fixed",
            "discount_value": "55",
            "tax_rate": "9",
            "due_date": "2099-12-31",
            "currency": "SGD",
        }
        res = client.post("/api/invoices",
                          json=payload,
                          content_type="application/json")
        assert res.status_code == 201, f"Status {res.status_code}: {res.get_json()}"
        data = res.get_json()
        inv = data["invoice"]
        assert inv["subtotal"]        == "300.00", f"subtotal {inv['subtotal']}"
        assert inv["discount_amount"] == "55.00",  f"discount {inv['discount_amount']}"
        assert inv["tax_amount"]      == "22.05",  f"tax {inv['tax_amount']}"
        assert inv["total_due"]       == "267.05", f"total {inv['total_due']}"

case("API: POST /api/invoices returns correct totals", test_api_create_invoice)


# -----------------------------------------------------------------------
# 14. API validation: bad discount rejected with 400
# -----------------------------------------------------------------------

def test_api_bad_discount_rejected():
    with app.test_client() as client:
        with app.app_context():
            init_db()
        payload = {
            "customer_name": "Test Co",
            "customer_email": "test@example.com",
            "description": "Test Service",
            "quantity": "3",
            "unit_price": "100",
            "discount_type": "fixed",
            "discount_value": "9999",   # exceeds subtotal
            "tax_rate": "9",
            "due_date": "2099-12-31",
        }
        res = client.post("/api/invoices", json=payload)
        assert res.status_code == 400, f"Expected 400, got {res.status_code}"

case("API: invalid discount returns 400", test_api_bad_discount_rejected)


# -----------------------------------------------------------------------
# 15. No discount AND no tax (baseline invoice)
# -----------------------------------------------------------------------

def test_no_discount_no_tax():
    r = compute_totals(5, Decimal("20.00"), "none", Decimal("0"), Decimal("0"))
    assert r["subtotal"]        == Decimal("100.00"), f"subtotal {r['subtotal']}"
    assert r["discount_amount"] == Decimal("0.00"),   f"discount {r['discount_amount']}"
    assert r["taxable_amount"]  == Decimal("100.00"), f"taxable {r['taxable_amount']}"
    assert r["tax_amount"]      == Decimal("0.00"),   f"tax {r['tax_amount']}"
    assert r["total_due"]       == Decimal("100.00"), f"total {r['total_due']}"

case("No discount, no tax -> total equals subtotal", test_no_discount_no_tax)


# -----------------------------------------------------------------------
# 16. $30 fixed discount with 0% tax
# -----------------------------------------------------------------------

def test_fixed_discount_zero_tax():
    r = compute_totals(2, Decimal("50.00"), "fixed", Decimal("30"), Decimal("0"))
    assert r["subtotal"]        == Decimal("100.00"), f"subtotal {r['subtotal']}"
    assert r["discount_amount"] == Decimal("30.00"),  f"discount {r['discount_amount']}"
    assert r["taxable_amount"]  == Decimal("70.00"),  f"taxable {r['taxable_amount']}"
    assert r["tax_amount"]      == Decimal("0.00"),   f"tax {r['tax_amount']}"
    assert r["total_due"]       == Decimal("70.00"),  f"total {r['total_due']}"

case("$30 fixed discount, 0% tax -> total 70.00", test_fixed_discount_zero_tax)


# -----------------------------------------------------------------------
# 17. Percentage discount with tax
#     qty=4, price=25 => subtotal=100; 15% discount=15; taxable=85; 8% tax=6.80; total=91.80
# -----------------------------------------------------------------------

def test_percentage_discount_with_tax():
    r = compute_totals(4, Decimal("25.00"), "percentage", Decimal("15"), Decimal("8"))
    assert r["subtotal"]        == Decimal("100.00"), f"subtotal {r['subtotal']}"
    assert r["discount_amount"] == Decimal("15.00"),  f"discount {r['discount_amount']}"
    assert r["taxable_amount"]  == Decimal("85.00"),  f"taxable {r['taxable_amount']}"
    assert r["tax_amount"]      == Decimal("6.80"),   f"tax {r['tax_amount']}"
    assert r["total_due"]       == Decimal("91.80"),  f"total {r['total_due']}"

case("Percentage 15% discount + 8% tax -> total 91.80", test_percentage_discount_with_tax)


# -----------------------------------------------------------------------
# 18. API: $30 fixed discount, 0% tax — verify taxable_amount is returned
# -----------------------------------------------------------------------

def test_api_fixed_discount_zero_tax():
    with app.test_client() as client:
        with app.app_context():
            init_db()
        payload = {
            "customer_name": "Discount Co",
            "customer_email": "dc@example.com",
            "description": "Service",
            "quantity": "2",
            "unit_price": "50",
            "discount_type": "fixed",
            "discount_value": "30",
            "tax_rate": "0",
            "due_date": "2099-12-31",
            "currency": "SGD",
        }
        res = client.post("/api/invoices", json=payload)
        assert res.status_code == 201, f"Status {res.status_code}: {res.get_json()}"
        inv = res.get_json()["invoice"]
        assert inv["subtotal"]        == "100.00", f"subtotal {inv['subtotal']}"
        assert inv["discount_amount"] == "30.00",  f"discount {inv['discount_amount']}"
        assert inv["taxable_amount"]  == "70.00",  f"taxable {inv['taxable_amount']}"
        assert inv["tax_amount"]      == "0.00",   f"tax {inv['tax_amount']}"
        assert inv["total_due"]       == "70.00",  f"total {inv['total_due']}"

case("API: $30 fixed discount, 0% tax -> total 70.00 with taxable_amount", test_api_fixed_discount_zero_tax)


# -----------------------------------------------------------------------
# 19. API: no discount, no tax (baseline invoice)
# -----------------------------------------------------------------------

def test_api_no_discount_no_tax():
    with app.test_client() as client:
        with app.app_context():
            init_db()
        payload = {
            "customer_name": "Plain Co",
            "customer_email": "plain@example.com",
            "description": "Plain Service",
            "quantity": "5",
            "unit_price": "20",
            "discount_type": "none",
            "discount_value": "0",
            "tax_rate": "0",
            "due_date": "2099-12-31",
            "currency": "SGD",
        }
        res = client.post("/api/invoices", json=payload)
        assert res.status_code == 201, f"Status {res.status_code}: {res.get_json()}"
        inv = res.get_json()["invoice"]
        assert inv["subtotal"]        == "100.00", f"subtotal {inv['subtotal']}"
        assert inv["discount_amount"] == "0.00",   f"discount {inv['discount_amount']}"
        assert inv["tax_amount"]      == "0.00",   f"tax {inv['tax_amount']}"
        assert inv["total_due"]       == "100.00", f"total {inv['total_due']}"

case("API: no discount, no tax -> total equals subtotal", test_api_no_discount_no_tax)


# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------

print()
if all_passed:
    print("All tests passed.")
    sys.exit(0)
else:
    print("Some tests FAILED.")
    sys.exit(1)
