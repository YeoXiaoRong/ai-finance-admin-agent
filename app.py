"""
AI Finance Admin Agent — Flask Backend
Handles invoice creation, PDF generation, and invoice history.
Designed to be extended with IBM watsonx Orchestrate API calls later.
"""

import hashlib
import hmac
import os
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from functools import wraps
from pathlib import Path

# Load .env file when present (no-op if the file doesn't exist or dotenv
# isn't installed — the app still works when env vars are set another way).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
# Standard Programme Catalog for Registration Automation
PROGRAMMES = {
    "AI Finance Workshop": {
        "description": "AI Finance Workshop — Practical Agent Automation",
        "fee": "300.00",
        "tax_rate": "9",
    },
    "Executive Leadership Masterclass": {
        "description": "Executive Leadership Masterclass — 2-Day Intensive",
        "fee": "850.00",
        "tax_rate": "9",
    },
    "Cloud Accounting Bootcamp": {
        "description": "Cloud Accounting Bootcamp — Hands-on Practice",
        "fee": "500.00",
        "tax_rate": "9",
    },
}
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    HRFlowable,
)

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
INVOICE_DIR = BASE_DIR / "generated_invoices"
INVOICE_DIR.mkdir(exist_ok=True)

DB_PATH = BASE_DIR / "invoices.db"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-in-prod")


def _get_public_base_url() -> str:
    """
    Return the public base URL used to build absolute download_url values.

    Reads PUBLIC_BASE_URL from the environment (set it in .env to your ngrok
    HTTPS address, e.g. "https://ai-finance-admin-agent.onrender.com".
    Trailing slashes are stripped.  Falls back to http://127.0.0.1:5000 for
    local development — never hardcoded in source.
    """
    raw = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    return raw if raw else "http://127.0.0.1:5000"


# ---------------------------------------------------------------------------
# Signed short-lived download tokens (for browser-clickable URLs)
# ---------------------------------------------------------------------------

_TOKEN_TTL_SECONDS = 600   # 10 minutes


def _token_signing_key() -> bytes:
    """
    Return the HMAC signing key for download tokens.

    Uses SECRET_KEY from the environment (already required for Flask session
    signing).  If SECRET_KEY is the insecure default, tokens still work but
    operators are expected to set a real value in .env before deploying.
    The key is encoded to bytes once here so callers always get bytes.
    """
    return app.config["SECRET_KEY"].encode()


def _make_download_token(invoice_number: str) -> str:
    """
    Create a signed, time-limited download token for one specific invoice.

    Token format (URL-safe, dot-separated):
        <invoice_number>.<expiry_unix_ts>.<hex_hmac>

    The invoice_number and expiry are included in the HMAC message so that
    a token issued for invoice A cannot be used to download invoice B, and
    a token cannot have its expiry extended without invalidating the signature.
    """
    expiry = int(time.time()) + _TOKEN_TTL_SECONDS
    message = f"{invoice_number}:{expiry}".encode()
    sig = hmac.new(_token_signing_key(), message, hashlib.sha256).hexdigest()
    return f"{invoice_number}.{expiry}.{sig}"


def _verify_download_token(token: str, invoice_number: str) -> tuple[bool, str]:
    """
    Verify a download token.

    Returns (True, "") on success.
    Returns (False, reason) on failure — reason is safe to log but NOT to
    return verbatim to the client (use a generic 'invalid or expired' message).

    Checks performed, in order:
      1. Token has exactly three dot-separated parts.
      2. The invoice_number embedded in the token matches the URL parameter.
      3. The token has not expired.
      4. The HMAC signature is correct (constant-time comparison).
    """
    parts = token.split(".")
    if len(parts) != 3:
        return False, "malformed token (wrong number of parts)"

    token_inv, expiry_str, provided_sig = parts

    if token_inv != invoice_number:
        return False, "token invoice mismatch"

    try:
        expiry = int(expiry_str)
    except ValueError:
        return False, "non-integer expiry"

    if time.time() > expiry:
        return False, "token expired"

    message = f"{invoice_number}:{expiry_str}".encode()
    expected_sig = hmac.new(_token_signing_key(), message, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        return False, "signature mismatch"

    return True, ""


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------

def _get_configured_key() -> str | None:
    """
    Return the FINANCE_API_KEY from the environment, or None if it is absent
    or blank.  Callers must treat None as 'server not configured' and reject
    the request — there is no unauthenticated dev-bypass.
    """
    value = os.environ.get("FINANCE_API_KEY", "").strip()
    return value if value else None


def require_api_key(f):
    """
    Decorator that enforces X-API-Key header authentication on every call.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        configured_key = _get_configured_key()
        if configured_key is None:
            return jsonify({
                "success": False,
                "error": (
                    "API authentication is not configured on this server. "
                    "Set the FINANCE_API_KEY environment variable and restart."
                ),
            }), 401
            
        provided_key = request.headers.get("X-API-Key", "").strip()
        
        
        if not provided_key:
            return jsonify({
                "success": False,
                "error": "Missing X-API-Key header.",
            }), 401
        if provided_key != configured_key:
            return jsonify({
                "success": False,
                "error": "Invalid API key.",
            }), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    """Return a per-request SQLite connection stored on Flask's g object."""
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the invoices table if it does not exist, and migrate if needed."""
    db = get_db()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number  TEXT    NOT NULL UNIQUE,
            customer_name   TEXT    NOT NULL,
            customer_email  TEXT    NOT NULL,
            description     TEXT    NOT NULL,
            quantity        INTEGER NOT NULL,
            unit_price      TEXT    NOT NULL,   -- stored as string to preserve Decimal precision
            subtotal        TEXT    NOT NULL,
            discount_type   TEXT    NOT NULL DEFAULT 'none',  -- 'none' | 'fixed' | 'percentage'
            discount_value  TEXT    NOT NULL DEFAULT '0',     -- raw input value
            discount_amount TEXT    NOT NULL DEFAULT '0',     -- computed monetary discount
            tax_rate        TEXT    NOT NULL DEFAULT '0',     -- percentage e.g. '9'
            tax_amount      TEXT    NOT NULL DEFAULT '0',     -- computed tax amount
            total_due       TEXT    NOT NULL DEFAULT '0',     -- taxable_amount + tax_amount
            currency        TEXT    NOT NULL DEFAULT 'SGD',
            due_date        TEXT    NOT NULL,
            issue_date      TEXT    NOT NULL,
            pdf_filename    TEXT,
            status          TEXT    NOT NULL DEFAULT 'unpaid',
            created_at      TEXT    NOT NULL
        )
        """
    )
    db.commit()
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS registrations (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_id     TEXT    NOT NULL UNIQUE,
            customer_name       TEXT    NOT NULL,
            customer_email      TEXT    NOT NULL,
            programme_name      TEXT    NOT NULL,
            quantity            INTEGER NOT NULL DEFAULT 1,
            fee                 TEXT    NOT NULL,
            status              TEXT    NOT NULL DEFAULT 'registered', -- 'registered' | 'invoiced'
            invoice_number      TEXT,
            created_at          TEXT    NOT NULL
        )
        """
    )
    db.commit() 
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id          TEXT    NOT NULL UNIQUE,
            invoice_number      TEXT    NOT NULL,
            amount_paid         TEXT    NOT NULL,  -- stored as string for Decimal precision
            payment_date        TEXT    NOT NULL,
            payment_method      TEXT    NOT NULL DEFAULT 'bank_transfer', -- 'bank_transfer' | 'paynow' | 'credit_card'
            reference_number    TEXT,
            status              TEXT    NOT NULL DEFAULT 'matched',       -- 'matched' | 'partial' | 'flagged' | 'unmatched'
            notes               TEXT,
            created_at          TEXT    NOT NULL,
            FOREIGN KEY (invoice_number) REFERENCES invoices(invoice_number)
        )
        """
    )
    db.commit()
    # Migrate existing table: add new columns if they do not exist yet
    existing_cols = {
        row[1] for row in db.execute("PRAGMA table_info(invoices)").fetchall()
    }
    migrations = [
        ("discount_type",   "TEXT NOT NULL DEFAULT 'none'"),
        ("discount_value",  "TEXT NOT NULL DEFAULT '0'"),
        ("discount_amount", "TEXT NOT NULL DEFAULT '0'"),
        ("tax_rate",        "TEXT NOT NULL DEFAULT '0'"),
        ("tax_amount",      "TEXT NOT NULL DEFAULT '0'"),
        ("total_due",       "TEXT NOT NULL DEFAULT '0'"),
    ]
    for col, defn in migrations:
        if col not in existing_cols:
            db.execute(f"ALTER TABLE invoices ADD COLUMN {col} {defn}")
    db.commit()

    # Back-fill total_due for rows created before this migration
    db.execute(
        "UPDATE invoices SET total_due = subtotal WHERE total_due = '0' AND subtotal != '0'"
    )
    db.commit()


# ---------------------------------------------------------------------------
# Invoice number generator
# ---------------------------------------------------------------------------

def make_invoice_number() -> str:
    """Generate a unique invoice number like INV-20240601-A3F2."""
    today = date.today().strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:4].upper()
    return f"INV-{today}-{suffix}"


# ---------------------------------------------------------------------------
# Calculation helper
# ---------------------------------------------------------------------------

TWO = Decimal("0.01")


def compute_totals(
    quantity: int,
    unit_price: Decimal,
    discount_type: str,
    discount_value: Decimal,
    tax_rate: Decimal,
) -> dict:
    """
    Returns a dict with subtotal, discount_amount, taxable_amount,
    tax_amount, and total_due, all as Decimal rounded to 2 d.p.

    discount_type: 'none' | 'fixed' | 'percentage'
    discount_value: the raw value (fixed amount or percentage 0-100)
    tax_rate: percentage (0-100)
    """
    subtotal = (unit_price * quantity).quantize(TWO, rounding=ROUND_HALF_UP)

    if discount_type == "fixed":
        discount_amount = discount_value.quantize(TWO, rounding=ROUND_HALF_UP)
    elif discount_type == "percentage":
        discount_amount = (subtotal * discount_value / Decimal("100")).quantize(
            TWO, rounding=ROUND_HALF_UP
        )
    else:
        discount_amount = Decimal("0.00")

    taxable_amount = (subtotal - discount_amount).quantize(TWO, rounding=ROUND_HALF_UP)
    tax_amount = (taxable_amount * tax_rate / Decimal("100")).quantize(
        TWO, rounding=ROUND_HALF_UP
    )
    total_due = (taxable_amount + tax_amount).quantize(TWO, rounding=ROUND_HALF_UP)

    return {
        "subtotal":       subtotal,
        "discount_amount": discount_amount,
        "taxable_amount":  taxable_amount,
        "tax_amount":      tax_amount,
        "total_due":       total_due,
    }


# ---------------------------------------------------------------------------
# PDF generation
# ---------------------------------------------------------------------------

def generate_invoice_pdf(invoice: dict) -> str:
    """
    Build a professional A4 PDF invoice using ReportLab.
    Returns the filename (not the full path).
    """
    filename = f"{invoice['invoice_number']}.pdf"
    filepath = INVOICE_DIR / filename

    doc = SimpleDocTemplate(
        str(filepath),
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    brand_blue = colors.HexColor("#1a56db")
    light_gray = colors.HexColor("#f3f4f6")
    dark_text = colors.HexColor("#111928")
    muted = colors.HexColor("#6b7280")

    style_h1 = ParagraphStyle(
        "H1", fontSize=26, textColor=brand_blue, fontName="Helvetica-Bold", spaceAfter=2
    )
    style_label = ParagraphStyle(
        "Label", fontSize=9, textColor=muted, fontName="Helvetica", spaceAfter=1
    )
    style_value = ParagraphStyle(
        "Value", fontSize=11, textColor=dark_text, fontName="Helvetica-Bold", spaceAfter=4
    )
    style_normal = ParagraphStyle(
        "Normal2", fontSize=10, textColor=dark_text, fontName="Helvetica", spaceAfter=2
    )
    style_footer = ParagraphStyle(
        "Footer", fontSize=8, textColor=muted, fontName="Helvetica",
        alignment=TA_CENTER
    )

    elements = []

    # ---- Header: company name + INVOICE label ----
    header_data = [
        [
            Paragraph("AI Finance Admin", style_h1),
            Paragraph("INVOICE", ParagraphStyle(
                "INV", fontSize=22, textColor=muted,
                fontName="Helvetica", alignment=TA_RIGHT
            )),
        ]
    ]
    header_table = Table(header_data, colWidths=[9 * cm, 8 * cm])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(header_table)
    elements.append(HRFlowable(width="100%", thickness=1, color=brand_blue, spaceAfter=12))

    # ---- Bill To + Invoice meta ----
    bill_to = [
        Paragraph("BILL TO", style_label),
        Paragraph(invoice["customer_name"], style_value),
        Paragraph(invoice["customer_email"], style_normal),
    ]
    meta = [
        Paragraph("INVOICE NUMBER", style_label),
        Paragraph(invoice["invoice_number"], style_value),
        Paragraph("ISSUE DATE", style_label),
        Paragraph(invoice["issue_date"], style_normal),
        Paragraph("DUE DATE", style_label),
        Paragraph(invoice["due_date"], style_value),
    ]
    info_data = [[bill_to, meta]]
    info_table = Table(info_data, colWidths=[9 * cm, 8 * cm])
    info_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.5 * cm))

    # ---- Line items table ----
    currency = invoice["currency"]
    col_headers = ["Description", "Qty", f"Unit Price ({currency})", f"Amount ({currency})"]
    subtotal = Decimal(invoice["subtotal"])
    line_total = (Decimal(invoice["unit_price"]) * invoice["quantity"]).quantize(
        TWO, rounding=ROUND_HALF_UP
    )

    items_data = [
        col_headers,
        [
            invoice["description"],
            str(invoice["quantity"]),
            f"{Decimal(invoice['unit_price']):.2f}",
            f"{line_total:.2f}",
        ],
    ]

    col_widths = [8.5 * cm, 2 * cm, 3.5 * cm, 3 * cm]
    items_table = Table(items_data, colWidths=col_widths, repeatRows=1)
    items_table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), brand_blue),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        # Data rows
        ("BACKGROUND", (0, 1), (-1, -1), light_gray),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, light_gray]),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
    ]))
    elements.append(items_table)
    elements.append(Spacer(1, 0.4 * cm))

    # ---- Totals block ----
    discount_amount = Decimal(invoice["discount_amount"])
    tax_rate_val = Decimal(invoice["tax_rate"])
    tax_amount = Decimal(invoice["tax_amount"])
    total_due = Decimal(invoice["total_due"])

    discount_type = invoice.get("discount_type", "none")
    discount_value = Decimal(invoice.get("discount_value", "0"))
    taxable_amount = Decimal(invoice.get("taxable_amount",
                                         str(subtotal - discount_amount)))

    # Build discount label
    if discount_type == "fixed":
        discount_label = "Discount (fixed)"
    elif discount_type == "percentage":
        discount_label = f"Discount ({discount_value:.2f}%)"
    else:
        discount_label = "Discount"

    tax_label = f"Tax ({tax_rate_val:.2f}%)"

    # Row construction:
    #   Subtotal
    #   Discount (only when > 0, in red)
    #   Amount after discount (only when a discount was applied)
    #   Tax (always — makes it clear tax was considered)
    #   TOTAL DUE
    totals_rows = [
        ["", "Subtotal", f"{currency} {subtotal:.2f}"],
    ]
    discount_row_idx = None
    after_discount_row_idx = None
    if discount_amount > Decimal("0"):
        discount_row_idx = len(totals_rows)
        totals_rows.append(["", discount_label, f"- {currency} {discount_amount:.2f}"])
        after_discount_row_idx = len(totals_rows)
        totals_rows.append(["", "Amount after discount", f"{currency} {taxable_amount:.2f}"])
    totals_rows.append(["", tax_label, f"{currency} {tax_amount:.2f}"])
    totals_rows.append(["", "TOTAL DUE", f"{currency} {total_due:.2f}"])

    total_row_idx = len(totals_rows) - 1

    subtotal_table = Table(totals_rows, colWidths=[9 * cm, 4 * cm, 4 * cm])

    table_style_cmds = [
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, total_row_idx - 1), "Helvetica"),
        ("FONTNAME", (0, total_row_idx), (-1, total_row_idx), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTSIZE", (0, total_row_idx), (-1, total_row_idx), 12),
        ("TEXTCOLOR", (0, total_row_idx), (-1, total_row_idx), brand_blue),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEABOVE", (1, total_row_idx), (-1, total_row_idx), 1, brand_blue),
    ]
    # Colour the discount row red; mute the "amount after discount" row slightly
    if discount_row_idx is not None:
        table_style_cmds += [
            ("TEXTCOLOR", (1, discount_row_idx), (-1, discount_row_idx),
             colors.HexColor("#c81e1e")),
            ("FONTNAME", (0, after_discount_row_idx), (-1, after_discount_row_idx),
             "Helvetica-Oblique"),
        ]
    subtotal_table.setStyle(TableStyle(table_style_cmds))

    elements.append(subtotal_table)

    elements.append(Spacer(1, 1 * cm))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e5e7eb")))
    elements.append(Spacer(1, 0.3 * cm))

    # ---- Footer note ----
    elements.append(Paragraph(
        "This invoice was generated by the AI Finance Admin Agent prototype. "
        "Payment status will be updated upon receipt of confirmed payment evidence.",
        style_footer,
    ))
    elements.append(Paragraph(
        f"Generated on {datetime.now().strftime('%d %b %Y %H:%M')}",
        style_footer,
    ))

    doc.build(elements)
    return filename


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------

def validate_invoice_input(data: dict) -> list[str]:
    """Return a list of validation error messages (empty list = valid)."""
    errors = []
    required_fields = [
        ("customer_name", "Customer name"),
        ("customer_email", "Customer email"),
        ("description", "Service description"),
        ("quantity", "Quantity"),
        ("unit_price", "Unit price"),
        ("due_date", "Due date"),
    ]
    for field, label in required_fields:
        if not data.get(field, "").strip():
            errors.append(f"{label} is required.")

    if errors:
        return errors  # Stop early — remaining checks need valid fields

    try:
        qty = int(data["quantity"])
        if qty <= 0:
            errors.append("Quantity must be greater than zero.")
    except ValueError:
        errors.append("Quantity must be a whole number.")

    unit_price_ok = True
    try:
        price = Decimal(data["unit_price"])
        if price < Decimal("0"):
            errors.append("Unit price cannot be negative.")
            unit_price_ok = False
    except InvalidOperation:
        errors.append("Unit price must be a valid number.")
        unit_price_ok = False

    # Discount validation
    discount_type = data.get("discount_type", "none").strip()
    if discount_type not in ("none", "fixed", "percentage"):
        errors.append("Discount type must be 'none', 'fixed', or 'percentage'.")
        discount_type = "none"

    if discount_type in ("fixed", "percentage"):
        raw_discount = data.get("discount_value", "0").strip() or "0"
        try:
            discount_value = Decimal(raw_discount)
            if discount_value < Decimal("0"):
                errors.append("Discount value cannot be negative.")
            elif discount_type == "percentage" and discount_value > Decimal("100"):
                errors.append("Percentage discount cannot exceed 100%.")
            elif discount_type == "fixed" and unit_price_ok:
                # Check fixed discount doesn't exceed subtotal
                try:
                    qty = int(data["quantity"])
                    price = Decimal(data["unit_price"])
                    computed_subtotal = (price * qty).quantize(TWO, rounding=ROUND_HALF_UP)
                    if discount_value > computed_subtotal:
                        errors.append(
                            f"Fixed discount ({discount_value:.2f}) cannot exceed "
                            f"the subtotal ({computed_subtotal:.2f})."
                        )
                except (InvalidOperation, ValueError):
                    pass  # Already reported above
        except InvalidOperation:
            errors.append("Discount value must be a valid number.")

    # Tax rate validation
    raw_tax = data.get("tax_rate", "0").strip() or "0"
    try:
        tax_rate = Decimal(raw_tax)
        if tax_rate < Decimal("0"):
            errors.append("Tax rate cannot be negative.")
    except InvalidOperation:
        errors.append("Tax rate must be a valid number.")

    try:
        datetime.strptime(data["due_date"], "%Y-%m-%d")
    except ValueError:
        errors.append("Due date must be in YYYY-MM-DD format.")

    return errors


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Render the main dashboard page."""
    with app.app_context():
        init_db()
    return render_template("index.html")

@app.route("/api/invoices", methods=["GET"])
def list_invoices():
    """
    Returns all stored invoices from SQLite.
    Compatible with both the frontend table and watsonx Orchestrate.
    """
    db = get_db()
    rows = db.execute(
        """
        SELECT 
            invoice_number, customer_name, customer_email, 
            total_due, due_date, status, created_at
        FROM invoices
        ORDER BY created_at DESC
        """
    ).fetchall()

    result = []
    for r in rows:
        inv_num = str(r["invoice_number"])
        pdf_file = os.path.join(INVOICE_STORAGE_DIR, f"{inv_num}.pdf") if 'INVOICE_STORAGE_DIR' in globals() else os.path.join("invoices", f"{inv_num}.pdf")
        
        result.append({
            "invoice_number": inv_num,
            "customer_name": str(r["customer_name"] or "N/A"),
            "customer_email": str(r["customer_email"] or ""),
            "total_due": str(r["total_due"] or "0.00"),
            "due_date": str(r["due_date"] or ""),
            "status": str(r["status"] or "unpaid"),
            "has_pdf": os.path.exists(pdf_file)
        })

    return jsonify(result), 200
@app.route("/api/invoices/<invoice_number>/download", methods=["GET"])
def download_invoice_pdf(invoice_number):
    """
    Direct browser download endpoint for invoice PDFs.
    """
    folder = INVOICE_STORAGE_DIR if 'INVOICE_STORAGE_DIR' in globals() else os.path.join(os.getcwd(), "invoices")
    filename = f"{invoice_number}.pdf"
    file_path = os.path.join(folder, filename)
    
    if not os.path.exists(file_path):
        return jsonify({"error": "Invoice PDF not found."}), 404
        
    return send_file(file_path, as_attachment=True, download_name=filename, mimetype="application/pdf")

@app.route("/api/invoices", methods=["POST"])
def create_invoice():
    """
    Create an invoice, generate its PDF, and persist it.
    Expects JSON body with: customer_name, customer_email, description,
    quantity, unit_price, due_date, currency (optional, defaults to SGD),
    discount_type ('none'|'fixed'|'percentage'), discount_value,
    tax_rate (percent).
    """
    data = request.get_json(force=True) or {}

    # Normalise all values to strings for validation
    str_data = {k: str(v).strip() for k, v in data.items()}

    errors = validate_invoice_input(str_data)
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    # Safe numeric parsing
    quantity = int(str_data["quantity"])
    unit_price = Decimal(str_data["unit_price"]).quantize(TWO, rounding=ROUND_HALF_UP)
    currency = str_data.get("currency", "SGD").upper() or "SGD"
    issue_date = date.today().strftime("%Y-%m-%d")
    invoice_number = make_invoice_number()

    discount_type = str_data.get("discount_type", "none") or "none"
    raw_discount = str_data.get("discount_value", "0") or "0"
    discount_value = Decimal(raw_discount).quantize(TWO, rounding=ROUND_HALF_UP)
    raw_tax = str_data.get("tax_rate", "0") or "0"
    tax_rate = Decimal(raw_tax).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    totals = compute_totals(quantity, unit_price, discount_type, discount_value, tax_rate)

    invoice = {
        "invoice_number":  invoice_number,
        "customer_name":   str_data["customer_name"],
        "customer_email":  str_data["customer_email"],
        "description":     str_data["description"],
        "quantity":        quantity,
        "unit_price":      str(unit_price),
        "subtotal":        str(totals["subtotal"]),
        "discount_type":   discount_type,
        "discount_value":  str(discount_value),
        "discount_amount": str(totals["discount_amount"]),
        "taxable_amount":  str(totals["taxable_amount"]),
        "tax_rate":        str(tax_rate),
        "tax_amount":      str(totals["tax_amount"]),
        "total_due":       str(totals["total_due"]),
        "currency":        currency,
        "due_date":        str_data["due_date"],
        "issue_date":      issue_date,
    }

    # Generate PDF
    pdf_filename = generate_invoice_pdf(invoice)
    invoice["pdf_filename"] = pdf_filename

    # Persist to DB
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    db = get_db()
    db.execute(
        """
        INSERT INTO invoices
          (invoice_number, customer_name, customer_email, description,
           quantity, unit_price, subtotal,
           discount_type, discount_value, discount_amount,
           tax_rate, tax_amount, total_due,
           currency, due_date, issue_date,
           pdf_filename, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            invoice["invoice_number"],
            invoice["customer_name"],
            invoice["customer_email"],
            invoice["description"],
            invoice["quantity"],
            invoice["unit_price"],
            invoice["subtotal"],
            invoice["discount_type"],
            invoice["discount_value"],
            invoice["discount_amount"],
            invoice["tax_rate"],
            invoice["tax_amount"],
            invoice["total_due"],
            invoice["currency"],
            invoice["due_date"],
            invoice["issue_date"],
            pdf_filename,
            "unpaid",
            now,
        ),
    )
    db.commit()

    base_url = _get_public_base_url()
    inv_num = invoice["invoice_number"]
    token = _make_download_token(inv_num)
    browser_download_url = f"{base_url}/api/invoices/{inv_num}/token-download/{token}"

    return jsonify({
        "success": True,
        "invoice": {
            **invoice,
            "status": "unpaid",
            "created_at": now,
            "browser_download_url": browser_download_url,
        },
        "browser_download_url": browser_download_url,
    }), 201


# ---------------------------------------------------------------------------
# Watsonx Orchestrate — Invoice generation endpoint
# ---------------------------------------------------------------------------
#
# Field name mapping: watsonx Orchestrate may send Title_Case or camelCase
# field names. This map normalises them to the internal snake_case keys that
# validate_invoice_input and compute_totals already understand.
#
_FIELD_ALIASES = {
    # Title_Case (as specified in the watsonx skill definition)
    "Customer_name":       "customer_name",
    "Customer_email":      "customer_email",
    "Service_description": "description",
    "Quantity":            "quantity",
    "Unit_price":          "unit_price",
    "Currency":            "currency",
    "Discount_type":       "discount_type",
    "Discount_value":      "discount_value",
    "Tax_rate":            "tax_rate",
    "Due_date":            "due_date",
    # camelCase variants
    "customerName":        "customer_name",
    "customerEmail":       "customer_email",
    "serviceDescription":  "description",
    "unitPrice":           "unit_price",
    "discountType":        "discount_type",
    "discountValue":       "discount_value",
    "taxRate":             "tax_rate",
    "dueDate":             "due_date",
}

# In-memory draft store  {token: invoice_dict}
# Drafts have no DB write and no PDF until confirmed.
# For production, replace with Redis or a DB-backed store.
_drafts: dict = {}


def _normalise_input(raw: dict) -> dict:
    """
    Return a new dict with all keys alias-resolved to snake_case.
    Unknown keys are passed through unchanged.
    """
    out = {}
    for k, v in raw.items():
        canonical = _FIELD_ALIASES.get(k) or _FIELD_ALIASES.get(k.strip()) or k
        out[canonical] = str(v).strip() if v is not None else ""
    # Ensure optional fields have a safe default when absent
    out.setdefault("discount_type",  "none")
    out.setdefault("discount_value", "0")
    out.setdefault("tax_rate",       "0")
    out.setdefault("currency",       "SGD")
    return out


def _build_invoice_dict(str_data: dict) -> dict:
    """
    Parse validated str_data, compute totals, and return a complete invoice
    dict ready for generate_invoice_pdf().  Does NOT touch the DB or filesystem.
    """
    quantity       = int(str_data["quantity"])
    unit_price     = Decimal(str_data["unit_price"]).quantize(TWO, rounding=ROUND_HALF_UP)
    currency       = str_data.get("currency", "SGD").upper() or "SGD"
    discount_type  = str_data.get("discount_type",  "none") or "none"
    discount_value = Decimal(str_data.get("discount_value", "0") or "0").quantize(
        TWO, rounding=ROUND_HALF_UP
    )
    tax_rate = Decimal(str_data.get("tax_rate", "0") or "0").quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )

    totals = compute_totals(quantity, unit_price, discount_type, discount_value, tax_rate)

    return {
        "invoice_number":  make_invoice_number(),
        "customer_name":   str_data["customer_name"],
        "customer_email":  str_data["customer_email"],
        "description":     str_data["description"],
        "quantity":        quantity,
        "unit_price":      str(unit_price),
        "subtotal":        str(totals["subtotal"]),
        "discount_type":   discount_type,
        "discount_value":  str(discount_value),
        "discount_amount": str(totals["discount_amount"]),
        "taxable_amount":  str(totals["taxable_amount"]),
        "tax_rate":        str(tax_rate),
        "tax_amount":      str(totals["tax_amount"]),
        "total_due":       str(totals["total_due"]),
        "currency":        currency,
        "due_date":        str_data["due_date"],
        "issue_date":      date.today().strftime("%Y-%m-%d"),
    }


def _finalise_invoice(invoice: dict):
    """Generate the PDF, persist to DB, and return the 201 success response."""
    pdf_filename = generate_invoice_pdf(invoice)
    invoice["pdf_filename"] = pdf_filename

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    db = get_db()
    db.execute(
        """
        INSERT INTO invoices
          (invoice_number, customer_name, customer_email, description,
           quantity, unit_price, subtotal,
           discount_type, discount_value, discount_amount,
           tax_rate, tax_amount, total_due,
           currency, due_date, issue_date,
           pdf_filename, status, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            invoice["invoice_number"],
            invoice["customer_name"],
            invoice["customer_email"],
            invoice["description"],
            invoice["quantity"],
            invoice["unit_price"],
            invoice["subtotal"],
            invoice["discount_type"],
            invoice["discount_value"],
            invoice["discount_amount"],
            invoice["tax_rate"],
            invoice["tax_amount"],
            invoice["total_due"],
            invoice["currency"],
            invoice["due_date"],
            invoice["issue_date"],
            pdf_filename,
            "unpaid",
            now,
        ),
    )
    db.commit()
# If this invoice originated from a registration, update the registration record
    reg_id = invoice.get("registration_id")
    if reg_id:
          db.execute(
              "UPDATE registrations SET status = 'invoiced', invoice_number = ? WHERE registration_id = ?",
              (invoice["invoice_number"], reg_id)
          )
    db.commit()
    base_url = _get_public_base_url()
    inv_num = invoice["invoice_number"]
    download_url = f"{base_url}/api/invoices/{inv_num}/download"

    token = _make_download_token(inv_num)
    browser_download_url = f"{base_url}/api/invoices/{inv_num}/token-download/{token}"

    return jsonify({
        "success":              True,
        "mode":                 "final",
        "invoice_number":       inv_num,
        "customer_name":        invoice["customer_name"],
        "currency":             invoice["currency"],
        "total_due":            invoice["total_due"],
        "pdf_filename":         pdf_filename,
        "status":               "Generated",
        "invoice": {
            **invoice,
            "status":     "unpaid",
            "created_at": now,
            "browser_download_url": browser_download_url,
        },
        "download_url":         download_url,
        "browser_download_url": browser_download_url,
        "message": (
            f"Invoice {pdf_filename} for {invoice['customer_name']} "
            f"({invoice['currency']} {invoice['total_due']}) has been generated. "
            f"Use browser_download_url for the user download link."
        ),
    }), 201


@app.route("/api/generate-invoice", methods=["POST"])
@require_api_key
def generate_invoice_api():
    """
    IBM watsonx Orchestrate — invoice generation endpoint.

    Accepts invoice fields in snake_case, Title_Case, or camelCase plus two
    control flags:

      draft (bool, default false)
        true  -> validate, compute totals, return a preview; no PDF, no DB write.
                 Response includes a draft_token for one-time confirmation.
        false -> validate, compute totals, generate PDF, persist to DB.

      confirm_token (string, optional)
        When provided with draft=false, the matching stored draft is finalised.
        Only the token is needed — no need to re-send all fields.

    Required fields:
      customer_name / Customer_name / customerName
      customer_email / Customer_email / customerEmail
      description / Service_description / serviceDescription
      quantity / Quantity
      unit_price / Unit_price / unitPrice
      due_date / Due_date / dueDate

    Optional fields (defaults shown):
      currency       SGD
      discount_type  none   (none | fixed | percentage)
      discount_value 0
      tax_rate       0

    Success response (draft=false, HTTP 201):
      {
        "success": true,
        "mode": "final",
        "invoice": { <all calculated fields> },
        "download_url": "/api/invoices/<number>/download"
      }

    Success response (draft=true, HTTP 200):
      {
        "success": true,
        "mode": "draft",
        "draft_token": "<token>",
        "preview": { <all calculated fields, no pdf_filename> },
        "message": "<instructions to confirm>"
      }
    """
    raw = request.get_json(force=True) or {}

    confirm_token = str(raw.get("confirm_token", "")).strip()
    draft_flag    = str(raw.get("draft", "false")).lower() in ("true", "1", "yes")

    # ---- Confirm a stored draft by token ----
    if confirm_token and not draft_flag:
        draft = _drafts.pop(confirm_token, None)   # single-use: remove on retrieval
        if draft is None:
            return jsonify({
                "success": False,
                "errors": [
                    f"Draft token '{confirm_token}' was not found or has already been used. "
                    "Re-submit the full invoice to create a new draft."
                ],
            }), 404
        return _finalise_invoice(draft)

    # ---- Parse and validate ----
    str_data = _normalise_input(raw)
    errors   = validate_invoice_input(str_data)
    if errors:
        return jsonify({"success": False, "errors": errors}), 400

    invoice = _build_invoice_dict(str_data)

    # ---- Draft / preview mode ----
    if draft_flag:
        token = uuid.uuid4().hex
        _drafts[token] = invoice
        return jsonify({
            "success":     True,
            "mode":        "draft",
            "draft_token": token,
            "preview":     invoice,
            "message": (
                "Review the preview above. When ready, POST to /api/generate-invoice "
                "with {\"draft\": false, \"confirm_token\": \"" + token + "\"} "
                "to generate the final PDF."
            ),
        }), 200

    # ---- Final mode: generate PDF + persist ----
    return _finalise_invoice(invoice)


@app.route("/api/invoices/<invoice_number>/download")
@require_api_key
def download_invoice(invoice_number: str):
    """
    Serve the PDF file for a given invoice number.
    Protected: requires X-API-Key header.

    Always responds with:
      Content-Type: application/pdf
      Content-Disposition: attachment; filename="<invoice_number>.pdf"

    The filename is derived from the invoice number (format INV-YYYYMMDD-XXXX)
    which contains only alphanumerics and hyphens — safe for all clients.
    """
    db = get_db()
    row = db.execute(
        "SELECT pdf_filename FROM invoices WHERE invoice_number = ?",
        (invoice_number,),
    ).fetchone()
    if row is None or not row["pdf_filename"]:
        abort(404)

    # Sanitise the download filename: keep only the invoice number stem
    # (alphanumerics and hyphens) and append .pdf — never trust DB content
    # directly as a header value.
    safe_stem = "".join(
        c for c in str(invoice_number) if c.isalnum() or c == "-"
    )
    safe_filename = f"{safe_stem}.pdf"

    filepath = INVOICE_DIR / row["pdf_filename"]
    if not filepath.is_file():
        abort(404)

    return send_file(
        str(filepath),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=safe_filename,
    )


@app.route("/api/invoices/<invoice_number>/token-download/<token>")
def download_invoice_by_token(invoice_number: str, token: str):
    """
    Public browser-clickable PDF download endpoint.

    No X-API-Key header is required.  Instead, the URL itself carries a
    cryptographically signed, short-lived token (valid 10 minutes) that is
    scoped to exactly this invoice number.

    The token is verified server-side:
      • Correct format
      • invoice_number embedded in token matches the URL path parameter
      • Not expired (10-minute TTL)
      • HMAC-SHA256 signature matches (constant-time comparison, SECRET_KEY)

    On any failure the response is an identical generic 403 — the reason is
    never disclosed to the caller to avoid oracle attacks.
    """
    valid, _reason = _verify_download_token(token, invoice_number)
    if not valid:
        return jsonify({
            "success": False,
            "error":   "This download link is invalid or has expired. "
                       "Generate a new invoice to get a fresh link.",
        }), 403

    db = get_db()
    row = db.execute(
        "SELECT pdf_filename FROM invoices WHERE invoice_number = ?",
        (invoice_number,),
    ).fetchone()
    if row is None or not row["pdf_filename"]:
        abort(404)

    safe_stem = "".join(
        c for c in str(invoice_number) if c.isalnum() or c == "-"
    )
    safe_filename = f"{safe_stem}.pdf"

    filepath = INVOICE_DIR / row["pdf_filename"]
    if not filepath.is_file():
        abort(404)

    return send_file(
        str(filepath),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=safe_filename,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Phase 2: Registration & Invoice Automation Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/programmes", methods=["GET"])
def list_programmes():
    """List available programmes and standard pricing."""
    return jsonify({
        "success": True,
        "programmes": [
            {
                "name": name,
                "description": info["description"],
                "fee": info["fee"],
                "currency": "SGD",
                "tax_rate": info["tax_rate"],
            }
            for name, info in PROGRAMMES.items()
        ]
    }), 200


@app.route("/api/registrations", methods=["GET"])
@require_api_key
def list_registrations():
    """List all registered participants."""
    db = get_db()
    rows = db.execute(
        "SELECT registration_id, customer_name, customer_email, programme_name, fee, status, invoice_number, created_at "
        "FROM registrations ORDER BY id DESC"
    ).fetchall()
    
    return jsonify({
        "success": True,
        "registrations": [
            {
                "registration_id": r["registration_id"],
                "customer_name": r["customer_name"],
                "customer_email": r["customer_email"],
                "programme_name": r["programme_name"],
                "fee": r["fee"],
                "status": r["status"],
                "invoice_number": r["invoice_number"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }), 200


@app.route("/api/register", methods=["POST"])
@require_api_key
def register_customer():
    """
    Register a customer for a programme and prepare an invoice draft.
    Validates input, checks duplicate registrations, records the registration,
    and returns an invoice draft with a confirmation_token for human approval.
    """
    data = request.get_json(silent=True) or {}
    
    name = (data.get("customer_name") or "").strip()
    email = (data.get("customer_email") or "").strip()
    prog_name = (data.get("programme_name") or "").strip()
    quantity = data.get("quantity", 1)
    
    # 1. Validation: Required fields
    if not name:
        return jsonify({"success": False, "error": "Customer name is required."}), 400
    if not email or "@" not in email or "." not in email:
        return jsonify({"success": False, "error": "A valid customer email is required."}), 400
    if not prog_name:
        return jsonify({"success": False, "error": "Programme name is required."}), 400

    # 2. Validation: Programme lookup
    programme = PROGRAMMES.get(prog_name)
    if not programme:
        available = ", ".join(PROGRAMMES.keys())
        return jsonify({
            "success": False,
            "error": f"Unknown programme '{prog_name}'. Available: {available}"
        }), 400

    try:
        qty_int = int(quantity)
        if qty_int <= 0:
            return jsonify({"success": False, "error": "Quantity must be at least 1."}), 400
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Quantity must be an integer."}), 400

    # 3. Check duplicate registration (same email + programme)
    db = get_db()
    existing = db.execute(
        "SELECT id FROM registrations WHERE customer_email = ? AND programme_name = ?",
        (email, prog_name)
    ).fetchone()
    if existing:
        return jsonify({
            "success": False,
            "error": f"{email} is already registered for {prog_name}."
        }), 400

    # 4. Record registration
    reg_id = f"REG-{uuid.uuid4().hex[:8].upper()}"
    now_str = datetime.now().isoformat()
    fee_str = str(programme["fee"])
    
    db.execute(
        """
        INSERT INTO registrations (registration_id, customer_name, customer_email, programme_name, quantity, fee, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'registered', ?)
        """,
        (reg_id, name, email, prog_name, qty_int, fee_str, now_str)
    )
    db.commit()

    # 5. Automatically create Invoice Draft using existing compute_totals
    due_date = data.get("due_date") or (date.today().replace(day=min(date.today().day + 14, 28))).isoformat()
    
    disc_type = data.get("discount_type", "none")
    disc_val = Decimal(str(data.get("discount_value", 0)))
    unit_price_dec = Decimal(str(programme["fee"]))
    tax_rate_dec = Decimal(str(programme["tax_rate"]))

    calc_result = compute_totals(
        quantity=qty_int,
        unit_price=unit_price_dec,
        discount_type=disc_type,
        discount_value=disc_val,
        tax_rate=tax_rate_dec
    )
    
    inv_number = make_invoice_number()
    issue_date = date.today().isoformat()

    # Store draft into existing _drafts dictionary
    token = uuid.uuid4().hex
    _drafts[token] = {
        "invoice_number": inv_number,
        "issue_date": issue_date,
        "customer_name": name,
        "customer_email": email,
        "description": f"Registration: {programme['description']}",
        "quantity": qty_int,
        "unit_price": str(unit_price_dec),
        "subtotal": str(calc_result["subtotal"]),
        "discount_type": disc_type,
        "discount_value": str(disc_val),
        "discount_amount": str(calc_result["discount_amount"]),
        "taxable_amount": str(calc_result["taxable_amount"]),
        "tax_rate": str(tax_rate_dec),
        "tax_amount": str(calc_result["tax_amount"]),
        "total_due": str(calc_result["total_due"]),
        "currency": "SGD",
        "due_date": due_date,
        "registration_id": reg_id,
        "created_at": time.time(),
    }

    return jsonify({
        "success": True,
        "message": f"Successfully registered {name} for {prog_name}. Invoice draft prepared.",
        "registration_id": reg_id,
        "draft_invoice": {
            "invoice_number": inv_number,
            "draft_token": token,
            "confirm_token": token,
            "customer_name": name,
            "customer_email": email,
            "programme": prog_name,
            "description": f"Registration: {programme['description']}",
            "quantity": qty_int,
            "unit_price": str(unit_price_dec),
            "subtotal": str(calc_result["subtotal"]),
            "discount_amount": str(calc_result["discount_amount"]),
            "tax_amount": str(calc_result["tax_amount"]),
            "total_due": str(calc_result["total_due"]),
            "currency": "SGD",
            "due_date": due_date,
        },
        "requires_approval": True,
        "next_step": "Present draft to user. Ask for confirmation before calling finalize_invoice."
    }), 201
def reconcile_invoice_payments(db, invoice_number: str) -> dict:
    """
    Computes total payments against an invoice and updates its status.
    Returns reconciliation summary.
    """
    inv = db.execute(
        "SELECT invoice_number, total_due, status FROM invoices WHERE invoice_number = ?",
        (invoice_number,)
    ).fetchone()

    if not inv:
        return {"status": "unmatched", "error": "Invoice not found"}

    total_due = Decimal(inv["total_due"])

    # Sum all payments for this invoice
    rows = db.execute(
        "SELECT amount_paid FROM payments WHERE invoice_number = ?",
        (invoice_number,)
    ).fetchall()

    total_paid = sum(Decimal(r["amount_paid"]) for r in rows)
    remaining = total_due - total_paid

    if total_paid == Decimal("0"):
        new_status = "unpaid"
        recon_status = "unpaid"
    elif total_paid < total_due:
        new_status = "partially_paid"
        recon_status = "partial"
    elif total_paid == total_due:
        new_status = "paid"
        recon_status = "matched"
    else:
        new_status = "overpaid"
        recon_status = "flagged"

    # Update invoice status
    db.execute(
        "UPDATE invoices SET status = ? WHERE invoice_number = ?",
        (new_status, invoice_number)
    )

    # If fully paid, also update any linked registration
    if new_status == "paid":
        db.execute(
            "UPDATE registrations SET status = 'paid' WHERE invoice_number = ?",
            (invoice_number,)
        )

    db.commit()

    return {
        "invoice_number": invoice_number,
        "total_due": str(total_due),
        "total_paid": str(total_paid),
        "remaining_balance": str(max(Decimal("0"), remaining)),
        "status": recon_status,
        "invoice_status": new_status,
    }


@app.route("/api/payments/record", methods=["POST"])
@require_api_key
def record_payment():
    """
    Records an incoming payment against an invoice and triggers reconciliation.
    """
    data = request.get_json(silent=True) or {}
    invoice_number = (data.get("invoice_number") or "").strip()
    amount_str = str(data.get("amount_paid", "")).strip()
    payment_method = (data.get("payment_method") or "bank_transfer").strip()
    reference_number = (data.get("reference_number") or "").strip()
    payment_date = (data.get("payment_date") or date.today().isoformat()).strip()

    if not invoice_number:
        return jsonify({"error": "Missing required field: invoice_number"}), 400

    try:
        amount_dec = Decimal(amount_str).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount_dec <= Decimal("0"):
            return jsonify({"error": "amount_paid must be greater than 0"}), 400
    except (InvalidOperation, TypeError):
        return jsonify({"error": "Invalid amount_paid format"}), 400

    db = get_db()
    inv = db.execute("SELECT id FROM invoices WHERE invoice_number = ?", (invoice_number,)).fetchone()
    if not inv:
        return jsonify({"error": f"Invoice {invoice_number} not found"}), 404

    payment_id = f"PAY-{date.today().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    now_iso = datetime.now(timezone.utc).isoformat()

    db.execute(
        """
        INSERT INTO payments (
            payment_id, invoice_number, amount_paid, payment_date,
            payment_method, reference_number, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'recorded', ?)
        """,
        (payment_id, invoice_number, str(amount_dec), payment_date, payment_method, reference_number, now_iso)
    )
    db.commit()

    recon_summary = reconcile_invoice_payments(db, invoice_number)

    # Update payment row with reconciliation status
    db.execute(
        "UPDATE payments SET status = ? WHERE payment_id = ?",
        (recon_summary["status"], payment_id)
    )
    db.commit()

    return jsonify({
        "success": True,
        "payment_id": payment_id,
        "reconciliation": recon_summary
    }), 201


@app.route("/api/reconciliation/status/<invoice_number>", methods=["GET"])
@require_api_key
def get_reconciliation_status(invoice_number: str):
    """
    Returns current payment and reconciliation breakdown for an invoice.
    """
    db = get_db()
    inv = db.execute(
        "SELECT invoice_number, customer_name, total_due, status FROM invoices WHERE invoice_number = ?",
        (invoice_number,)
    ).fetchone()

    if not inv:
        return jsonify({"error": f"Invoice {invoice_number} not found"}), 404

    payments = db.execute(
        "SELECT payment_id, amount_paid, payment_date, payment_method, reference_number, status FROM payments WHERE invoice_number = ? ORDER BY id ASC",
        (invoice_number,)
    ).fetchall()

    recon_summary = reconcile_invoice_payments(db, invoice_number)

    return jsonify({
        "success": True,
        "invoice": {
            "invoice_number": inv["invoice_number"],
            "customer_name": inv["customer_name"],
            "total_due": inv["total_due"],
            "status": inv["status"]
        },
        "reconciliation": recon_summary,
        "payments": [dict(p) for p in payments]
    }), 200

@app.route("/api/reconciliation/aging-report", methods=["GET"])
@require_api_key
def get_invoice_aging_report():
    """
    Scans all unpaid or partially paid invoices, calculates aging days past due,
    and categorizes them into standard aging buckets (Current, 1-30, 31-60, 60+).
    """
    db = get_db()
    today = date.today()

    rows = db.execute(
        """
        SELECT 
            i.invoice_number, i.customer_name, i.customer_email, 
            i.description, i.total_due, i.due_date, i.status,
            COALESCE(SUM(p.amount_paid), '0.00') AS total_paid
        FROM invoices i
        LEFT JOIN payments p ON i.invoice_number = p.invoice_number
        WHERE i.status IN ('unpaid', 'partially_paid', 'overpaid')
        GROUP BY i.invoice_number
        ORDER BY i.due_date ASC
        """
    ).fetchall()

    aging_summary = {
        "current": [],
        "1_30_days": [],
        "31_60_days": [],
        "60_plus_days": [],
        "overpaid_flagged": []
    }

    for row in rows:
        total_due = Decimal(row["total_due"])
        total_paid = Decimal(str(row["total_paid"]))
        balance = max(Decimal("0.00"), total_due - total_paid)

        if row["status"] == "overpaid":
            aging_summary["overpaid_flagged"].append({
                "invoice_number": row["invoice_number"],
                "customer_name": row["customer_name"],
                "customer_email": row["customer_email"],
                "total_due": str(total_due),
                "total_paid": str(total_paid),
                "status": "overpaid",
                "notes": "Flagged for manual credit memo or refund review."
            })
            continue

        try:
            due_d = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
            days_overdue = (today - due_d).days
        except (ValueError, TypeError):
            days_overdue = 0

        item = {
            "invoice_number": row["invoice_number"],
            "customer_name": row["customer_name"],
            "customer_email": row["customer_email"],
            "description": row["description"],
            "total_due": str(total_due),
            "total_paid": str(total_paid),
            "outstanding_balance": str(balance),
            "due_date": row["due_date"],
            "days_overdue": max(0, days_overdue)
        }

        if days_overdue <= 0:
            aging_summary["current"].append(item)
        elif 1 <= days_overdue <= 30:
            item["escalation_level"] = "gentle_reminder"
            aging_summary["1_30_days"].append(item)
        elif 31 <= days_overdue <= 60:
            item["escalation_level"] = "formal_demand"
            aging_summary["31_60_days"].append(item)
        else:
            item["escalation_level"] = "admin_hold"
            aging_summary["60_plus_days"].append(item)

    return jsonify({
        "success": True,
        "as_of_date": today.isoformat(),
        "summary": {
            "current_count": len(aging_summary["current"]),
            "1_30_days_count": len(aging_summary["1_30_days"]),
            "31_60_days_count": len(aging_summary["31_60_days"]),
            "60_plus_days_count": len(aging_summary["60_plus_days"]),
            "overpaid_count": len(aging_summary["overpaid_flagged"])
        },
        "aging_buckets": aging_summary
    }), 200


@app.route("/api/reconciliation/escalate", methods=["POST"])
@require_api_key
def trigger_escalation_notices():
    """
    Generates actionable escalation notices and email drafts tailored
    to each overdue invoice's aging severity tier.
    """
    data = request.get_json(silent=True) or {}
    target_invoice = (data.get("invoice_number") or "").strip()

    db = get_db()
    today = date.today()

    query = """
        SELECT 
            i.invoice_number, i.customer_name, i.customer_email, 
            i.description, i.total_due, i.due_date, i.status,
            COALESCE(SUM(p.amount_paid), '0.00') AS total_paid
        FROM invoices i
        LEFT JOIN payments p ON i.invoice_number = p.invoice_number
        WHERE i.status IN ('unpaid', 'partially_paid')
    """
    params = []
    if target_invoice:
        query += " AND i.invoice_number = ?"
        params.append(target_invoice)

    query += " GROUP BY i.invoice_number ORDER BY i.due_date ASC"

    rows = db.execute(query, tuple(params)).fetchall()
    notices = []

    for row in rows:
        total_due = Decimal(row["total_due"])
        total_paid = Decimal(str(row["total_paid"]))
        balance = max(Decimal("0.00"), total_due - total_paid)

        if balance <= Decimal("0.00"):
            continue

        try:
            due_d = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
            days_overdue = (today - due_d).days
        except (ValueError, TypeError):
            days_overdue = 0

        if days_overdue <= 0:
            continue

        inv_num = row["invoice_number"]
        cust_name = row["customer_name"]
        cust_email = row["customer_email"]

        if 1 <= days_overdue <= 30:
            subject = f"Friendly Reminder: Outstanding Payment for Invoice {inv_num}"
            urgency = "low"
            message_body = (
                f"Dear {cust_name},\n\n"
                f"We hope this email finds you well. This is a gentle reminder that invoice {inv_num} "
                f"for {row['description']} was due on {row['due_date']} ({days_overdue} days ago).\n\n"
                f"Outstanding Balance: SGD {balance}\n\n"
                f"If you have already arranged payment, please disregard this note. Otherwise, "
                f"kindly arrange settlement via PayNow or bank transfer at your earliest convenience."
            )
        elif 31 <= days_overdue <= 60:
            subject = f"Second Notice: Overdue Balance for Invoice {inv_num}"
            urgency = "medium"
            message_body = (
                f"Dear {cust_name},\n\n"
                f"Our records show that invoice {inv_num} remains unpaid and is now {days_overdue} days past due.\n\n"
                f"Outstanding Balance: SGD {balance}\n"
                f"Original Due Date: {row['due_date']}\n\n"
                f"Please remit payment immediately to keep your account in good standing. "
                f"If you require payment details or an updated statement, please let us know."
            )
        else:
            subject = f"FINAL NOTICE: Account Suspension Warning - Invoice {inv_num}"
            urgency = "high"
            message_body = (
                f"Dear {cust_name},\n\n"
                f"Despite previous notices, invoice {inv_num} is critically overdue by {days_overdue} days.\n\n"
                f"Amount Due: SGD {balance}\n\n"
                f"Please settle this balance immediately. Unsettled accounts past 60 days are subject to "
                f"registration holds and escalation to our finance management team."
            )

        notices.append({
            "invoice_number": inv_num,
            "customer_name": cust_name,
            "customer_email": cust_email,
            "days_overdue": days_overdue,
            "outstanding_balance": str(balance),
            "urgency": urgency,
            "email_subject": subject,
            "email_body": message_body
        })

    return jsonify({
        "success": True,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "total_notices_generated": len(notices),
        "escalations": notices
    }), 200

# Fixed conversion table relative to base currency SGD
FX_RATES_TO_SGD = {
    "SGD": Decimal("1.0000"),
    "USD": Decimal("1.3450"),
    "EUR": Decimal("1.4620"),
    "GBP": Decimal("1.7100"),
    "MYR": Decimal("0.2850")
}

@app.route("/api/fx-rates", methods=["GET"])
def get_fx_rates():
    """
    Returns supported currency conversion rates relative to SGD.
    Public endpoint so watsonx can query exchange rates.
    """
    return jsonify({
        "success": True,
        "base_currency": "SGD",
        "rates": {k: str(v) for k, v in FX_RATES_TO_SGD.items()},
        "last_updated": date.today().isoformat()
    }), 200


@app.route("/api/payments/multi-currency-record", methods=["POST"])
@require_api_key
def record_multi_currency_payment():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    
    inv_num = str(data.get("invoice_number") or "").strip()
    raw_amount = data.get("amount_paid")
    currency = str(data.get("currency") or "SGD").strip().upper()
    payment_method = str(data.get("payment_method") or "bank_transfer").strip()
    ref_num = str(data.get("reference_number") or "").strip()
    payment_date_str = str(data.get("payment_date") or date.today().isoformat()).strip()

    if not inv_num or raw_amount is None or str(raw_amount).strip() == "":
        return jsonify({"success": False, "error": "Missing invoice_number or amount_paid."}), 400

    if currency not in FX_RATES_TO_SGD:
        return jsonify({
            "success": False, 
            "error": f"Unsupported currency '{currency}'. Supported currencies: {list(FX_RATES_TO_SGD.keys())}"
        }), 400

    try:
        foreign_amount = Decimal(str(raw_amount).strip())
        if foreign_amount <= Decimal("0.00"):
            raise ValueError
    except Exception:
        return jsonify({"success": False, "error": "Invalid positive payment amount."}), 400

    rate = FX_RATES_TO_SGD[currency]
    sgd_equivalent = (foreign_amount * rate).quantize(Decimal("0.01"))

    db = get_db()
    inv = db.execute("SELECT * FROM invoices WHERE invoice_number = ?", (inv_num,)).fetchone()
    if not inv:
        return jsonify({"success": False, "error": f"Invoice {inv_num} not found."}), 404

    pay_id = f"PAY-FX-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    db.execute(
        """
        INSERT INTO payments (
            payment_id, invoice_number, amount_paid, payment_method, 
            reference_number, payment_date, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pay_id,
            inv_num,
            str(sgd_equivalent),
            f"{payment_method}_{currency.lower()}",
            f"{ref_num} ({currency} {foreign_amount} @ {rate})",
            payment_date_str,
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        )
    )

    total_due = Decimal(str(inv["total_due"]))
    paid_sum = db.execute(
        "SELECT COALESCE(SUM(CAST(amount_paid AS NUMERIC)), 0) as total FROM payments WHERE invoice_number = ?",
        (inv_num,)
    ).fetchone()["total"]
    total_paid = Decimal(str(paid_sum)).quantize(Decimal("0.01"))

    if total_paid >= total_due:
        new_status = "paid" if total_paid == total_due else "overpaid"
    elif total_paid > Decimal("0.00"):
        new_status = "partially_paid"
    else:
        new_status = "unpaid"

    db.execute("UPDATE invoices SET status = ? WHERE invoice_number = ?", (new_status, inv_num))
    db.commit()

    return jsonify({
        "success": True,
        "payment_id": pay_id,
        "fx_conversion": {
            "original_currency": currency,
            "original_amount": f"{foreign_amount:.2f}",
            "exchange_rate_to_sgd": str(rate),
            "sgd_credited": f"{sgd_equivalent:.2f}"
        },
        "reconciliation": {
            "invoice_number": inv_num,
            "total_due_sgd": f"{total_due:.2f}",
            "total_paid_sgd": f"{total_paid:.2f}",
            "remaining_balance_sgd": f"{max(Decimal('0.00'), total_due - total_paid):.2f}",
            "invoice_status": new_status
        }
    }), 201
@app.route("/api/demo/seed", methods=["POST", "GET"])
def seed_demo_data():
    """
    Seeds synthetic records across all financial lifecycle stages
    for evaluation without private credentials.
    """
    db = get_db()
    db.execute("DELETE FROM payments")
    db.execute("DELETE FROM registrations")
    db.execute("DELETE FROM invoices")

    today_str = date.today().isoformat()

    # 1. Settled Corporate Invoice (USD Multi-Currency)
    db.execute("""
        INSERT INTO invoices (
            invoice_number, customer_name, customer_email, description,
            quantity, unit_price, subtotal, total_due, issue_date, due_date, status, created_at
        ) VALUES (
            'INV-DEMO-2026-01', 'Acme Global Corp', 'billing@acme.com',
            'AI Finance Automation Workshop (Corporate Tier)', 2, '500.00', '1000.00', '1000.00',
            '2026-10-01', '2026-11-30', 'paid', '2026-10-01 09:00:00'
        )
    """)
    db.execute("""
        INSERT INTO payments (
            payment_id, invoice_number, amount_paid, payment_method, reference_number, payment_date, created_at
        ) VALUES (
            'PAY-DEMO-101', 'INV-DEMO-2026-01', '1000.00', 'bank_transfer_usd', 'WIRE-USD-743 (USD 743.49 @ 1.3450)',
            '2026-10-05', '2026-10-05 14:30:00'
        )
    """)

    # 2. Partially Settled Invoice (USD Conversion in progress)
    db.execute("""
        INSERT INTO invoices (
            invoice_number, customer_name, customer_email, description,
            quantity, unit_price, subtotal, total_due, issue_date, due_date, status, created_at
        ) VALUES (
            'INV-DEMO-2026-02', 'TechVentures Pte Ltd', 'finance@techventures.io',
            'Enterprise AI Agent Deployment Advisory', 1, '850.00', '850.00', '850.00',
            '2026-10-10', '2026-11-15', 'partially_paid', '2026-10-10 11:15:00'
        )
    """)
    db.execute("""
        INSERT INTO payments (
            payment_id, invoice_number, amount_paid, payment_method, reference_number, payment_date, created_at
        ) VALUES (
            'PAY-DEMO-102', 'INV-DEMO-2026-02', '403.50', 'bank_transfer_usd', 'WIRE-USD-300 (USD 300.00 @ 1.3450)',
            '2026-10-15', '2026-10-15 16:45:00'
        )
    """)

    # 3. Overdue Invoice (Triggers Aging Buckets & Escalation Engine)
    db.execute("""
        INSERT INTO invoices (
            invoice_number, customer_name, customer_email, description,
            quantity, unit_price, subtotal, total_due, issue_date, due_date, status, created_at
        ) VALUES (
            'INV-DEMO-2026-03', 'Solaria Logistics', 'accounts@solaria.sg',
            'Executive Masterclass: Cloud & AI Integration', 1, '1200.00', '1200.00', '1200.00',
            '2026-09-01', '2026-09-15', 'unpaid', '2026-09-01 08:30:00'
        )
    """)
    db.commit()

    return jsonify({
        "success": True,
        "message": "Demo test environment populated with 3 synthetic corporate accounts (Paid, Partially Paid, Overdue)."
    }), 200
if __name__ == "__main__":
    with app.app_context():
        init_db()
    # Use Waitress as the production WSGI server.
    # debug=False, the interactive debugger, and the reloader are all off.
    from waitress import serve
    print("Starting AI Finance Admin on http://127.0.0.1:5000")
    serve(app, host="0.0.0.0", port=5000)