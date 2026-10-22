"""
Tests for POST /api/generate-invoice and API key authentication.

Covers:
  - Authentication: missing key, wrong key, correct key, no-key dev mode
  - Protected routes: GET /api/invoices, POST /api/generate-invoice,
    GET /api/invoices/<n>/download
  - Unprotected route: GET / (web UI)
  - Unprotected route: POST /api/invoices (web-form route)
  - Final mode calculations (no discount, $55 fixed + 9% tax, $30 fixed + 0%,
    10% percentage + 9%)
  - Validation errors (excessive discount, missing fields, negative tax)
  - Field-name aliases (Title_Case, camelCase)
  - Draft mode preview
  - Draft -> confirm flow (token single-use)
  - Unknown token -> 404
  - download_url resolves to a real PDF

Run with:
    python test_generate_invoice_api.py
(from the ai-finance-admin directory)
"""

import os
import sys
import time
sys.path.insert(0, os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# We test two modes:
#   AUTH MODE  — FINANCE_API_KEY is set; protected routes require the header.
#   NO-KEY MODE — env var absent; all routes open (dev convenience).
# We set/unset the env var before importing app so each test class gets the
# right behaviour.  We re-import by clearing sys.modules between the two runs.
# ---------------------------------------------------------------------------

TEST_KEY = "test-secret-key-abc123"

# ---- helpers ---------------------------------------------------------------

all_passed = True

def run(label, fn):
    global all_passed
    try:
        fn()
        print(f"  PASS  {label}")
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        all_passed = False


BASE = {
    "customer_name":  "Acme Pte Ltd",
    "customer_email": "billing@acme.com",
    "description":    "Consulting Services",
    "quantity":       "3",
    "unit_price":     "100",
    "due_date":       "2099-12-31",
}

# Shorthand: post to /api/generate-invoice with optional headers
def api_post(client, body, headers=None):
    return client.post("/api/generate-invoice",
                       json=body,
                       headers=headers or {})

def with_key(key=TEST_KEY):
    return {"X-API-Key": key}

# ===========================================================================
# Part 1 — Authentication behaviour (FINANCE_API_KEY is set)
# ===========================================================================

print("\n--- Part 1: Auth mode (FINANCE_API_KEY set) ---")

os.environ["FINANCE_API_KEY"] = TEST_KEY

# Fresh import with the key set
import importlib
import app as _app_module
importlib.reload(_app_module)
from app import app as flask_app, init_db, _drafts

def auth_client():
    with flask_app.app_context():
        init_db()
    return flask_app.test_client()


# 1. Missing key -> 401 on /api/generate-invoice
def test_missing_key_generate_invoice():
    c = auth_client()
    res = api_post(c, BASE)          # no X-API-Key
    assert res.status_code == 401, f"Expected 401, got {res.status_code}"
    body = res.get_json()
    assert body["success"] is False
    assert "Missing" in body["error"] or "missing" in body["error"].lower()

run("Auth: missing X-API-Key -> 401 on POST /api/generate-invoice",
    test_missing_key_generate_invoice)


# 2. Wrong key -> 401
def test_wrong_key_generate_invoice():
    c = auth_client()
    res = api_post(c, BASE, {"X-API-Key": "wrong-key"})
    assert res.status_code == 401, f"Expected 401, got {res.status_code}"
    body = res.get_json()
    assert body["success"] is False
    assert "Invalid" in body["error"] or "invalid" in body["error"].lower()

run("Auth: wrong X-API-Key -> 401 on POST /api/generate-invoice",
    test_wrong_key_generate_invoice)


# 3. Missing key -> 401 on GET /api/invoices
def test_missing_key_list_invoices():
    c = auth_client()
    res = c.get("/api/invoices")
    assert res.status_code == 401, f"Expected 401, got {res.status_code}"

run("Auth: missing key -> 401 on GET /api/invoices", test_missing_key_list_invoices)


# 4. Missing key -> 401 on download endpoint
def test_missing_key_download():
    c = auth_client()
    # First create an invoice with the correct key so we have a real number
    res = api_post(c, BASE, with_key())
    assert res.status_code == 201
    inv_number = res.get_json()["invoice"]["invoice_number"]
    # Now try download without key
    dl = c.get(f"/api/invoices/{inv_number}/download")
    assert dl.status_code == 401, f"Expected 401, got {dl.status_code}"

run("Auth: missing key -> 401 on GET /api/invoices/<n>/download",
    test_missing_key_download)


# 5. Correct key -> 200 on GET /api/invoices
def test_correct_key_list_invoices():
    c = auth_client()
    res = c.get("/api/invoices", headers=with_key())
    assert res.status_code == 200, f"Expected 200, got {res.status_code}"
    assert isinstance(res.get_json(), list)

run("Auth: correct key -> 200 on GET /api/invoices", test_correct_key_list_invoices)


# 6. Correct key -> PDF download works
def test_correct_key_download():
    c = auth_client()
    res = api_post(c, BASE, with_key())
    assert res.status_code == 201
    inv_number = res.get_json()["invoice"]["invoice_number"]
    dl = c.get(f"/api/invoices/{inv_number}/download", headers=with_key())
    assert dl.status_code == 200
    assert dl.content_type == "application/pdf"

run("Auth: correct key -> 200 + PDF on GET /api/invoices/<n>/download",
    test_correct_key_download)


# 7. GET / (web UI) is never gated — no key needed
def test_web_ui_always_open():
    c = auth_client()
    res = c.get("/")
    assert res.status_code == 200, f"GET / returned {res.status_code}"

run("Auth: GET / (web UI) is always accessible without a key", test_web_ui_always_open)


# 8. POST /api/invoices (web-form route) is not gated
def test_web_form_route_open():
    c = auth_client()
    res = c.post("/api/invoices", json=BASE)
    # Must not be 401; it might be 201 (success) or 400 (validation) but not auth-blocked
    assert res.status_code != 401, \
        f"POST /api/invoices should not require an API key (web form route), got 401"

run("Auth: POST /api/invoices (web form) is not blocked by API key",
    test_web_form_route_open)


# ===========================================================================
# Part 2 — No-key mode (FINANCE_API_KEY unset): ALL protected routes -> 401
# ===========================================================================
#
# There is no unauthenticated bypass.  When FINANCE_API_KEY is not set the
# server rejects every protected request with HTTP 401 and a configuration
# error message, making it impossible to accidentally run unprotected.

print("\n--- Part 2: No-key mode (FINANCE_API_KEY unset) ---")

# Blank the variable so _get_configured_key() returns None.
# We set it to "" rather than deleting it so that load_dotenv() inside
# the module reload cannot re-populate it from .env (load_dotenv does not
# override existing env vars by default, but an empty string still counts
# as "set", which our decorator treats as unconfigured).
os.environ["FINANCE_API_KEY"] = ""
importlib.reload(_app_module)
from app import app as flask_app_nokey, init_db as init_db_nokey, _drafts as _drafts_nokey

def nokey_client():
    with flask_app_nokey.app_context():
        init_db_nokey()
    return flask_app_nokey.test_client()


# 9. No key configured -> POST /api/generate-invoice is blocked with 401
def test_nokey_mode_generate_invoice_blocked():
    c = nokey_client()
    res = api_post(c, BASE)          # no X-API-Key header
    assert res.status_code == 401, \
        f"Expected 401 when FINANCE_API_KEY unset, got {res.status_code}"
    body = res.get_json()
    assert body["success"] is False
    # The server must return a configuration-error message (not a generic
    # "missing header" message) when FINANCE_API_KEY itself is blank/absent.
    assert "configured" in body["error"].lower(), \
        f"Expected configuration error, got: {body['error']!r}"

run("No-key mode: POST /api/generate-invoice blocked with 401 when FINANCE_API_KEY unset",
    test_nokey_mode_generate_invoice_blocked)


# 10. No key configured -> GET /api/invoices is also blocked with 401
def test_nokey_mode_list_invoices_blocked():
    c = nokey_client()
    res = c.get("/api/invoices")
    assert res.status_code == 401, \
        f"Expected 401 when FINANCE_API_KEY unset, got {res.status_code}"

run("No-key mode: GET /api/invoices blocked with 401 when FINANCE_API_KEY unset",
    test_nokey_mode_list_invoices_blocked)


# 10b. No key configured -> even supplying the right key string is still rejected
#      (server has no key to compare against, so it can't validate any key)
def test_nokey_mode_any_key_blocked():
    c = nokey_client()
    res = api_post(c, BASE, with_key())   # correct key value, but server unset
    assert res.status_code == 401, \
        f"Expected 401 even with header when FINANCE_API_KEY unset, got {res.status_code}"

run("No-key mode: even sending a key header is blocked when FINANCE_API_KEY unset",
    test_nokey_mode_any_key_blocked)


# 10c. Web UI is still reachable even when FINANCE_API_KEY is unset
def test_nokey_mode_web_ui_open():
    c = nokey_client()
    res = c.get("/")
    assert res.status_code == 200, f"GET / should always be accessible, got {res.status_code}"

run("No-key mode: GET / (web UI) accessible even when FINANCE_API_KEY unset",
    test_nokey_mode_web_ui_open)


# ===========================================================================
# Part 3 — Calculation and functional tests (key set, correct header used)
# ===========================================================================

print("\n--- Part 3: Calculation & functional tests ---")

os.environ["FINANCE_API_KEY"] = TEST_KEY
importlib.reload(_app_module)
from app import app as flask_app2, init_db as init_db2, _drafts as _drafts2

def c2():
    with flask_app2.app_context():
        init_db2()
    return flask_app2.test_client()


# 11. No discount, no tax -> total = subtotal
def test_final_no_discount_no_tax():
    cl = c2()
    res = api_post(cl, BASE, with_key())
    assert res.status_code == 201
    inv = res.get_json()["invoice"]
    assert inv["subtotal"]        == "300.00"
    assert inv["discount_amount"] == "0.00"
    assert inv["tax_amount"]      == "0.00"
    assert inv["total_due"]       == "300.00"

run("Final: no discount, no tax -> total equals subtotal (300.00)",
    test_final_no_discount_no_tax)


# 12. $55 fixed discount + 9% tax -> 267.05
def test_final_fixed55_tax9():
    cl = c2()
    res = api_post(cl, {**BASE, "discount_type": "fixed",
                         "discount_value": "55", "tax_rate": "9"}, with_key())
    assert res.status_code == 201
    inv = res.get_json()["invoice"]
    assert inv["subtotal"]        == "300.00"
    assert inv["discount_amount"] == "55.00"
    assert inv["taxable_amount"]  == "245.00"
    assert inv["tax_amount"]      == "22.05"
    assert inv["total_due"]       == "267.05"

run("Final: $55 fixed discount + 9% tax -> 267.05", test_final_fixed55_tax9)


# 13. $30 fixed discount + 0% tax -> 270.00
def test_final_fixed30_tax0():
    cl = c2()
    res = api_post(cl, {**BASE, "discount_type": "fixed",
                         "discount_value": "30", "tax_rate": "0"}, with_key())
    assert res.status_code == 201
    inv = res.get_json()["invoice"]
    assert inv["total_due"] == "270.00"

run("Final: $30 fixed discount, 0% tax -> 270.00", test_final_fixed30_tax0)


# 14. 10% percentage discount + 9% tax -> 294.30
def test_final_pct10_tax9():
    cl = c2()
    res = api_post(cl, {**BASE, "discount_type": "percentage",
                         "discount_value": "10", "tax_rate": "9"}, with_key())
    assert res.status_code == 201
    inv = res.get_json()["invoice"]
    assert inv["discount_amount"] == "30.00"
    assert inv["taxable_amount"]  == "270.00"
    assert inv["tax_amount"]      == "24.30"
    assert inv["total_due"]       == "294.30"

run("Final: 10% percentage discount + 9% tax -> 294.30", test_final_pct10_tax9)


# 15. Excessive discount -> 400
def test_validation_discount_exceeds_subtotal():
    cl = c2()
    res = api_post(cl, {**BASE, "discount_type": "fixed",
                         "discount_value": "9999"}, with_key())
    assert res.status_code == 400
    assert res.get_json()["success"] is False

run("Validation: excessive fixed discount -> 400", test_validation_discount_exceeds_subtotal)


# 16. Missing required fields -> 400
def test_validation_missing_fields():
    cl = c2()
    res = api_post(cl, {}, with_key())
    assert res.status_code == 400
    assert len(res.get_json()["errors"]) >= 5

run("Validation: empty body -> 400 with field errors", test_validation_missing_fields)


# 17. Negative tax -> 400
def test_validation_negative_tax():
    cl = c2()
    res = api_post(cl, {**BASE, "tax_rate": "-5"}, with_key())
    assert res.status_code == 400

run("Validation: negative tax rate -> 400", test_validation_negative_tax)


# 18. Title_Case field names (watsonx style)
def test_title_case_fields():
    cl = c2()
    res = api_post(cl, {
        "Customer_name":       "Title Case Co",
        "Customer_email":      "tc@example.com",
        "Service_description": "Title Case Test",
        "Quantity":            "2",
        "Unit_price":          "50",
        "Due_date":            "2099-12-31",
        "Discount_type":       "fixed",
        "Discount_value":      "10",
        "Tax_rate":            "7",
        "Currency":            "USD",
    }, with_key())
    assert res.status_code == 201, f"{res.status_code}: {res.get_json()}"
    inv = res.get_json()["invoice"]
    assert inv["subtotal"]        == "100.00"
    assert inv["discount_amount"] == "10.00"
    assert inv["taxable_amount"]  == "90.00"
    assert inv["tax_amount"]      == "6.30"
    assert inv["total_due"]       == "96.30"
    assert inv["currency"]        == "USD"

run("Title_Case field names accepted and calculated correctly", test_title_case_fields)


# 19. camelCase field names
def test_camel_case_fields():
    cl = c2()
    res = api_post(cl, {
        "customerName":       "CamelCase Co",
        "customerEmail":      "cc@example.com",
        "serviceDescription": "camelCase test",
        "quantity":           "4",
        "unitPrice":          "25",
        "dueDate":            "2099-06-30",
        "discountType":       "percentage",
        "discountValue":      "20",
        "taxRate":            "0",
    }, with_key())
    assert res.status_code == 201, f"{res.status_code}: {res.get_json()}"
    inv = res.get_json()["invoice"]
    assert inv["total_due"] == "80.00"

run("camelCase field names accepted and calculated correctly", test_camel_case_fields)


# 20. Draft mode: no PDF, token issued
def test_draft_mode():
    cl = c2()
    _drafts2.clear()
    res = api_post(cl, {**BASE, "draft": "true",
                         "discount_type": "fixed",
                         "discount_value": "55",
                         "tax_rate": "9"}, with_key())
    assert res.status_code == 200
    body = res.get_json()
    assert body["mode"] == "draft"
    assert "draft_token" in body
    preview = body["preview"]
    assert preview["total_due"]  == "267.05"
    assert "pdf_filename" not in preview or preview.get("pdf_filename") is None
    assert body["draft_token"] in _drafts2

run("Draft mode: preview returned with correct totals, token stored, no PDF",
    test_draft_mode)


# 21. Draft requires correct key too
def test_draft_requires_key():
    cl = c2()
    res = api_post(cl, {**BASE, "draft": "true"})   # no key
    assert res.status_code == 401

run("Auth: draft mode also requires correct X-API-Key", test_draft_requires_key)


# 22. Draft -> confirm flow
def test_draft_confirm_flow():
    cl = c2()
    _drafts2.clear()
    res1 = api_post(cl, {**BASE, "draft": True,
                          "discount_type": "fixed",
                          "discount_value": "55",
                          "tax_rate": "9"}, with_key())
    assert res1.status_code == 200
    token = res1.get_json()["draft_token"]

    res2 = api_post(cl, {"draft": False, "confirm_token": token}, with_key())
    assert res2.status_code == 201, f"Confirm failed: {res2.status_code} {res2.get_json()}"
    body2 = res2.get_json()
    inv = body2["invoice"]
    assert inv["total_due"]  == "267.05"
    assert inv["pdf_filename"].endswith(".pdf")
    assert token not in _drafts2          # token consumed

run("Draft -> confirm flow produces correct invoice and consumes token",
    test_draft_confirm_flow)


# 23. Confirm also requires correct key
def test_confirm_requires_key():
    cl = c2()
    _drafts2.clear()
    res1 = api_post(cl, {**BASE, "draft": True}, with_key())
    token = res1.get_json()["draft_token"]
    # Confirm without key
    res2 = api_post(cl, {"draft": False, "confirm_token": token})
    assert res2.status_code == 401

run("Auth: confirm_token also requires correct X-API-Key", test_confirm_requires_key)


# 24. Unknown token -> 404
def test_unknown_token():
    cl = c2()
    res = api_post(cl, {"draft": False,
                         "confirm_token": "nonexistent000"}, with_key())
    assert res.status_code == 404
    assert res.get_json()["success"] is False

run("Unknown confirm_token -> 404", test_unknown_token)


# 25. Token single-use
def test_token_single_use():
    cl = c2()
    _drafts2.clear()
    res1 = api_post(cl, {**BASE, "draft": True}, with_key())
    token = res1.get_json()["draft_token"]
    res2 = api_post(cl, {"draft": False, "confirm_token": token}, with_key())
    assert res2.status_code == 201
    res3 = api_post(cl, {"draft": False, "confirm_token": token}, with_key())
    assert res3.status_code == 404

run("Draft token is single-use: second confirmation -> 404", test_token_single_use)


# 26. download_url is absolute (starts with http) and resolves to a real PDF
def test_download_url_is_absolute_and_reachable():
    cl = c2()
    res = api_post(cl, BASE, with_key())
    assert res.status_code == 201
    body = res.get_json()
    url = body["download_url"]

    # Must be an absolute URL (not a bare path)
    assert url.startswith("http"), \
        f"download_url should be absolute, got: {url!r}"
    # Must contain the invoice number
    inv_number = body["invoice"]["invoice_number"]
    assert inv_number in url, \
        f"invoice_number {inv_number!r} not found in download_url {url!r}"

    # Strip host so Flask test client can GET the path directly
    path = "/" + url.split("/", 3)[-1]   # e.g. /api/invoices/INV-.../download
    dl = cl.get(path, headers=with_key())
    assert dl.status_code == 200
    assert dl.content_type == "application/pdf"

run("download_url is absolute, contains invoice number, resolves to PDF",
    test_download_url_is_absolute_and_reachable)


# 27. PUBLIC_BASE_URL is used when set
def test_public_base_url_used():
    # Temporarily set PUBLIC_BASE_URL and reload the module
    os.environ["PUBLIC_BASE_URL"] = "https://example.ngrok-free.dev"
    importlib.reload(_app_module)
    from app import app as _app_tmp, init_db as _init_tmp
    with _app_tmp.app_context():
        _init_tmp()
    cl_tmp = _app_tmp.test_client()
    res = cl_tmp.post("/api/generate-invoice",
                      json=BASE,
                      headers=with_key())
    assert res.status_code == 201
    url = res.get_json()["download_url"]
    assert url.startswith("https://example.ngrok-free.dev"), \
        f"Expected PUBLIC_BASE_URL prefix, got: {url!r}"
    # Restore for subsequent tests
    os.environ["PUBLIC_BASE_URL"] = ""
    importlib.reload(_app_module)

run("PUBLIC_BASE_URL env var is used as download_url prefix", test_public_base_url_used)


# 28. download_url falls back to localhost when PUBLIC_BASE_URL unset
def test_public_base_url_fallback():
    # Set to "" rather than deleting — load_dotenv() won't override an already-set
    # (even blank) env var, so this prevents .env from re-populating the value.
    saved = os.environ.get("PUBLIC_BASE_URL")
    os.environ["PUBLIC_BASE_URL"] = ""
    importlib.reload(_app_module)
    from app import app as _app_tmp, init_db as _init_tmp
    with _app_tmp.app_context():
        _init_tmp()
    cl_tmp = _app_tmp.test_client()
    res = cl_tmp.post("/api/generate-invoice",
                      json=BASE,
                      headers=with_key())
    assert res.status_code == 201
    url = res.get_json()["download_url"]
    assert url.startswith("http://127.0.0.1:5000"), \
        f"Expected localhost fallback, got: {url!r}"
    # Restore original value (or remove if it wasn't set before)
    if saved is not None:
        os.environ["PUBLIC_BASE_URL"] = saved
    else:
        os.environ.pop("PUBLIC_BASE_URL", None)
    importlib.reload(_app_module)

run("download_url falls back to http://127.0.0.1:5000 when PUBLIC_BASE_URL unset",
    test_public_base_url_fallback)


# 29. Content-Disposition header is set correctly on download
def test_download_content_disposition():
    cl = c2()
    res = api_post(cl, BASE, with_key())
    assert res.status_code == 201
    inv_number = res.get_json()["invoice"]["invoice_number"]
    path = f"/api/invoices/{inv_number}/download"
    dl = cl.get(path, headers=with_key())
    assert dl.status_code == 200
    assert dl.content_type == "application/pdf"
    cd = dl.headers.get("Content-Disposition", "")
    assert "attachment" in cd, f"Expected attachment disposition, got: {cd!r}"
    assert inv_number in cd or inv_number.replace("-", "") in cd, \
        f"Invoice number not in Content-Disposition: {cd!r}"

run("Download: Content-Type=application/pdf and Content-Disposition=attachment set",
    test_download_content_disposition)


# 30. Download without key returns 401 (not the PDF)
def test_download_no_key_returns_401():
    cl = c2()
    res = api_post(cl, BASE, with_key())
    inv_number = res.get_json()["invoice"]["invoice_number"]
    dl = cl.get(f"/api/invoices/{inv_number}/download")   # no X-API-Key
    assert dl.status_code == 401
    assert dl.content_type.startswith("application/json")
    assert dl.get_json()["success"] is False

run("Download without X-API-Key returns 401 JSON (not the PDF)",
    test_download_no_key_returns_401)


# 31. Download with unknown invoice number returns 404
def test_download_unknown_invoice_returns_404():
    cl = c2()
    dl = cl.get("/api/invoices/INV-00000000-ZZZZ/download", headers=with_key())
    assert dl.status_code == 404

run("Download of unknown invoice number returns 404", test_download_unknown_invoice_returns_404)


# ===========================================================================
# Part 4 — Signed browser download tokens
# ===========================================================================
# These tests use the same flask_app2 / c2() client (FINANCE_API_KEY set).
# We also import the token helpers directly to craft manipulated tokens.

print("\n--- Part 4: Signed browser download tokens ---")

# Re-import helpers after restoring env to TEST_KEY state
os.environ["FINANCE_API_KEY"] = TEST_KEY
importlib.reload(_app_module)
from app import (
    app as flask_app4,
    init_db as init_db4,
    _make_download_token,
    _verify_download_token,
    _TOKEN_TTL_SECONDS,
)

def c4():
    with flask_app4.app_context():
        init_db4()
    return flask_app4.test_client()


# 32. Final invoice response contains browser_download_url
def test_browser_download_url_present():
    cl = c4()
    res = api_post(cl, BASE, with_key())
    assert res.status_code == 201
    body = res.get_json()
    assert "browser_download_url" in body, \
        "browser_download_url missing from response"
    bdl = body["browser_download_url"]
    assert bdl.startswith("http"), f"Expected absolute URL, got: {bdl!r}"
    assert "token-download" in bdl, \
        f"Expected token-download path segment, got: {bdl!r}"

run("Final invoice response contains absolute browser_download_url",
    test_browser_download_url_present)


# 33. Valid browser_download_url serves the PDF without X-API-Key
def test_valid_token_download():
    cl = c4()
    res = api_post(cl, BASE, with_key())
    assert res.status_code == 201
    body = res.get_json()
    inv_number = body["invoice"]["invoice_number"]
    bdl = body["browser_download_url"]
    # Extract just the path (strip scheme + host)
    path = "/" + bdl.split("/", 3)[-1]

    # No X-API-Key header — simulates a browser click
    dl = cl.get(path)
    assert dl.status_code == 200, \
        f"Expected 200 for valid token, got {dl.status_code}: {dl.get_data(as_text=True)[:200]}"
    assert dl.content_type == "application/pdf"
    cd = dl.headers.get("Content-Disposition", "")
    assert "attachment" in cd
    assert inv_number in cd or inv_number.replace("-", "") in cd

run("Valid browser_download_url serves PDF with correct headers (no X-API-Key needed)",
    test_valid_token_download)


# 34. Expired token returns 403
def test_expired_token_returns_403():
    cl = c4()
    res = api_post(cl, BASE, with_key())
    inv_number = res.get_json()["invoice"]["invoice_number"]

    # Build a token whose expiry is in the past
    import hashlib as _hl, hmac as _hm
    past_expiry = int(time.time()) - 1
    msg = f"{inv_number}:{past_expiry}".encode()
    sk  = flask_app4.config["SECRET_KEY"].encode()
    sig = _hm.new(sk, msg, _hl.sha256).hexdigest()
    stale_token = f"{inv_number}.{past_expiry}.{sig}"

    dl = cl.get(f"/api/invoices/{inv_number}/token-download/{stale_token}")
    assert dl.status_code == 403, \
        f"Expected 403 for expired token, got {dl.status_code}"
    body = dl.get_json()
    assert body["success"] is False
    assert "expired" in body["error"].lower() or "invalid" in body["error"].lower()

run("Expired token returns 403 with generic error", test_expired_token_returns_403)


# 35. Tampered signature returns 403
def test_tampered_signature_returns_403():
    cl = c4()
    res = api_post(cl, BASE, with_key())
    inv_number = res.get_json()["invoice"]["invoice_number"]

    good_token = _make_download_token(inv_number)
    inv_part, expiry_part, sig_part = good_token.split(".")
    # Flip the last hex character
    bad_sig = sig_part[:-1] + ("0" if sig_part[-1] != "0" else "1")
    tampered_token = f"{inv_part}.{expiry_part}.{bad_sig}"

    dl = cl.get(f"/api/invoices/{inv_number}/token-download/{tampered_token}")
    assert dl.status_code == 403, \
        f"Expected 403 for tampered token, got {dl.status_code}"

run("Tampered HMAC signature returns 403", test_tampered_signature_returns_403)


# 36. Token issued for invoice A cannot download invoice B
def test_token_scoped_to_invoice():
    cl = c4()
    # Create two invoices
    res_a = api_post(cl, {**BASE, "description": "Invoice A"}, with_key())
    res_b = api_post(cl, {**BASE, "description": "Invoice B"}, with_key())
    assert res_a.status_code == 201
    assert res_b.status_code == 201

    inv_a = res_a.get_json()["invoice"]["invoice_number"]
    inv_b = res_b.get_json()["invoice"]["invoice_number"]

    # Get a valid token for invoice A
    token_for_a = _make_download_token(inv_a)

    # Try to use invoice A's token on invoice B's endpoint
    dl = cl.get(f"/api/invoices/{inv_b}/token-download/{token_for_a}")
    assert dl.status_code == 403, \
        f"Token for A must not download B, got {dl.status_code}"

run("Token scoped to invoice A cannot be used to download invoice B",
    test_token_scoped_to_invoice)


# 37. Malformed token (wrong number of segments) returns 403
def test_malformed_token_returns_403():
    cl = c4()
    res = api_post(cl, BASE, with_key())
    inv_number = res.get_json()["invoice"]["invoice_number"]

    for bad in ["notavalidtoken", "only.two", "a.b.c.d.e"]:
        dl = cl.get(f"/api/invoices/{inv_number}/token-download/{bad}")
        assert dl.status_code == 403, \
            f"Malformed token {bad!r} should return 403, got {dl.status_code}"

run("Malformed tokens (wrong segment count) all return 403", test_malformed_token_returns_403)


# 38. Existing API-key-protected download endpoint is unchanged
def test_api_key_download_still_works():
    cl = c4()
    res = api_post(cl, BASE, with_key())
    inv_number = res.get_json()["invoice"]["invoice_number"]

    # With key → 200
    dl = cl.get(f"/api/invoices/{inv_number}/download", headers=with_key())
    assert dl.status_code == 200
    assert dl.content_type == "application/pdf"

    # Without key → 401
    dl_no_key = cl.get(f"/api/invoices/{inv_number}/download")
    assert dl_no_key.status_code == 401

run("Existing API-key-protected download still works; no key still returns 401",
    test_api_key_download_still_works)


# 39. _verify_download_token unit tests
def test_verify_token_unit():
    with flask_app4.app_context():
        inv = "INV-20991231-TEST"
        good = _make_download_token(inv)
        ok, reason = _verify_download_token(good, inv)
        assert ok, f"Valid token rejected: {reason}"

        # Wrong invoice in path
        ok2, _ = _verify_download_token(good, "INV-20991231-XXXX")
        assert not ok2

        # Corrupt expiry
        parts = good.split(".")
        bad_expiry = f"{parts[0]}.notanint.{parts[2]}"
        ok3, _ = _verify_download_token(bad_expiry, inv)
        assert not ok3

        # Truncated token
        ok4, _ = _verify_download_token("short.token", inv)
        assert not ok4

run("_verify_download_token unit tests: valid passes, all invalid cases rejected",
    test_verify_token_unit)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print()
if all_passed:
    print("All tests passed.")
    sys.exit(0)
else:
    print("Some tests FAILED.")
    sys.exit(1)
