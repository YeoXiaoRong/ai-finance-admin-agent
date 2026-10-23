/**
 * AI Finance Admin Agent — Frontend Logic
 * Handles: live totals, client-side validation, confirmation modal,
 * form submission, invoice history rendering, PDF download.
 */

"use strict";

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------
const form          = document.getElementById("invoiceForm");
const previewBtn    = document.getElementById("previewBtn");
const cancelBtn     = document.getElementById("cancelBtn");
const confirmBtn    = document.getElementById("confirmBtn");
const closeSuccessBtn = document.getElementById("closeSuccessBtn");

const confirmModal  = document.getElementById("confirmModal");
const successModal  = document.getElementById("successModal");
const previewGrid   = document.getElementById("previewGrid");
const successMsg    = document.getElementById("successMsg");
const downloadLink  = document.getElementById("downloadLink");

const historyBody   = document.getElementById("historyBody");
const emptyRow      = document.getElementById("emptyRow");
const toast         = document.getElementById("toast");

// Totals strip elements
const subtotalValue  = document.getElementById("subtotalValue");
const discountRow    = document.getElementById("discountRow");
const discountRowLabel = document.getElementById("discountRowLabel");
const discountValue  = document.getElementById("discountValue");
const taxValue       = document.getElementById("taxValue");
const totalDueValue  = document.getElementById("totalDueValue");

// Discount controls
const discountTypeEl  = document.getElementById("discount_type");
const discountValueEl = document.getElementById("discount_value");
const discountValueUnit = document.getElementById("discountValueUnit");

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

/** Show a toast message above the form. */
function showToast(message, type = "error") {
  toast.textContent = message;
  toast.className = `toast ${type}`;
  toast.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.add("hidden"), 6000);
}

/** Format a number as a currency string, e.g. "SGD 1,200.00" */
function formatCurrency(amount, currency) {
  const num = parseFloat(amount);
  if (isNaN(num)) return "—";
  return `${currency} ${num.toLocaleString("en-SG", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Return today's date in YYYY-MM-DD for the due date min attribute. */
function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

/** Read all form values into a plain object. */
function readForm() {
  return {
    customer_name:  form.customer_name.value.trim(),
    customer_email: form.customer_email.value.trim(),
    description:    form.description.value.trim(),
    quantity:       form.quantity.value.trim(),
    unit_price:     form.unit_price.value.trim(),
    currency:       form.currency.value,
    discount_type:  form.discount_type.value,
    discount_value: form.discount_value.value.trim() || "0",
    tax_rate:       form.tax_rate.value.trim() || "0",
    due_date:       form.due_date.value,
  };
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

/** Set minimum due date to today so past dates are blocked. */
function initDueDate() {
  const dueDateInput = document.getElementById("due_date");
  dueDateInput.min = todayISO();
}


// ---------------------------------------------------------------------------
// Discount type toggle
// ---------------------------------------------------------------------------

function onDiscountTypeChange() {
  const type = discountTypeEl.value;
  if (type === "none") {
    discountValueEl.disabled = true;
    discountValueEl.value = "";
    discountValueUnit.textContent = "";
  } else if (type === "fixed") {
    discountValueEl.disabled = false;
    discountValueEl.placeholder = "0.00";
    discountValueEl.step = "0.01";
    const curr = document.getElementById("currency").value;
    discountValueUnit.textContent = `(${curr})`;
  } else if (type === "percentage") {
    discountValueEl.disabled = false;
    discountValueEl.placeholder = "0";
    discountValueEl.step = "0.01";
    discountValueUnit.textContent = "(%)";
  }
  updateTotals();
}

discountTypeEl.addEventListener("change", onDiscountTypeChange);
// Also update unit hint when currency changes
document.getElementById("currency").addEventListener("change", () => {
  if (discountTypeEl.value === "fixed") {
    discountValueUnit.textContent = `(${document.getElementById("currency").value})`;
  }
});

// ---------------------------------------------------------------------------
// Live totals calculation (mirrors backend compute_totals)
// ---------------------------------------------------------------------------

function updateTotals() {
  const qty   = parseFloat(document.getElementById("quantity").value);
  const price = parseFloat(document.getElementById("unit_price").value);
  const curr  = document.getElementById("currency").value;
  const discType  = discountTypeEl.value;
  const discInput = parseFloat(discountValueEl.value) || 0;
  const taxInput  = parseFloat(document.getElementById("tax_rate").value) || 0;

  if (!isNaN(qty) && qty > 0 && !isNaN(price) && price >= 0) {
    const subtotal = round2(qty * price);

    let discountAmt = 0;
    let discountLabel = "";
    if (discType === "fixed") {
      discountAmt = round2(Math.max(0, discInput));
      discountLabel = `Discount (fixed)`;
    } else if (discType === "percentage") {
      discountAmt = round2(subtotal * Math.max(0, Math.min(100, discInput)) / 100);
      discountLabel = `Discount (${discInput.toFixed(2)}%)`;
    }

    const taxable  = round2(subtotal - discountAmt);
    const taxAmt   = round2(taxable * Math.max(0, taxInput) / 100);
    const totalDue = round2(taxable + taxAmt);

    subtotalValue.textContent = formatCurrency(subtotal, curr);
    taxValue.textContent = `${formatCurrency(taxAmt, curr)} (${taxInput.toFixed(2)}%)`;
    totalDueValue.textContent = formatCurrency(totalDue, curr);

    if (discountAmt > 0) {
      discountRowLabel.textContent = discountLabel;
      discountValue.textContent = `- ${formatCurrency(discountAmt, curr)}`;
      discountRow.classList.remove("hidden");
    } else {
      discountRow.classList.add("hidden");
    }
  } else {
    subtotalValue.textContent = "—";
    discountRow.classList.add("hidden");
    taxValue.textContent = "—";
    totalDueValue.textContent = "—";
  }
}

/** Round to 2 decimal places using ROUND_HALF_UP approximation. */
function round2(val) {
  return Math.round((val + Number.EPSILON) * 100) / 100;
}

document.getElementById("quantity").addEventListener("input", updateTotals);
document.getElementById("unit_price").addEventListener("input", updateTotals);
document.getElementById("currency").addEventListener("change", updateTotals);
discountValueEl.addEventListener("input", updateTotals);
document.getElementById("tax_rate").addEventListener("input", updateTotals);

// ---------------------------------------------------------------------------
// Client-side validation
// ---------------------------------------------------------------------------

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Validate form fields. Returns true if valid, false otherwise. */
function validateForm(data) {
  let valid = true;
  clearErrors();

  function setError(fieldId, message) {
    const el = document.getElementById(fieldId);
    const err = document.getElementById("err_" + fieldId);
    if (el)  el.classList.add("invalid");
    if (err) err.textContent = message;
    valid = false;
  }

  if (!data.customer_name)
    setError("customer_name", "Customer name is required.");

  if (!data.customer_email)
    setError("customer_email", "Customer email is required.");
  else if (!EMAIL_RE.test(data.customer_email))
    setError("customer_email", "Enter a valid email address.");

  if (!data.description)
    setError("description", "Service description is required.");

  if (!data.quantity) {
    setError("quantity", "Quantity is required.");
  } else {
    const qty = Number(data.quantity);
    if (!Number.isInteger(qty) || qty <= 0)
      setError("quantity", "Quantity must be a whole number greater than zero.");
  }

  if (!data.unit_price) {
    setError("unit_price", "Unit price is required.");
  } else {
    const price = parseFloat(data.unit_price);
    if (isNaN(price) || price < 0)
      setError("unit_price", "Unit price cannot be negative.");
  }

  // Discount validation
  if (data.discount_type !== "none") {
    const discVal = parseFloat(data.discount_value);
    if (isNaN(discVal) || discVal < 0) {
      setError("discount_value", "Discount value cannot be negative.");
    } else if (data.discount_type === "percentage" && discVal > 100) {
      setError("discount_value", "Percentage discount cannot exceed 100%.");
    } else if (data.discount_type === "fixed") {
      // Check against subtotal
      const qty = parseFloat(data.quantity);
      const price = parseFloat(data.unit_price);
      if (!isNaN(qty) && !isNaN(price)) {
        const subtotal = round2(qty * price);
        if (discVal > subtotal) {
          setError("discount_value",
            `Fixed discount (${discVal.toFixed(2)}) cannot exceed subtotal (${subtotal.toFixed(2)}).`);
        }
      }
    }
  }

  // Tax rate validation
  const taxRate = parseFloat(data.tax_rate);
  if (isNaN(taxRate) || taxRate < 0)
    setError("tax_rate", "Tax rate cannot be negative.");

  if (!data.due_date)
    setError("due_date", "Due date is required.");

  return valid;
}

function clearErrors() {
  document.querySelectorAll(".field-error").forEach(el => (el.textContent = ""));
  document.querySelectorAll("input.invalid, select.invalid").forEach(el => el.classList.remove("invalid"));
  toast.classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Confirmation modal
// ---------------------------------------------------------------------------

function openConfirmModal(data) {
  const qty      = parseInt(data.quantity, 10);
  const price    = parseFloat(data.unit_price);
  const subtotal = round2(qty * price);

  // Compute discount
  let discountAmt = 0;
  let discountLabel = "Discount";
  if (data.discount_type === "fixed") {
    discountAmt = round2(parseFloat(data.discount_value) || 0);
    discountLabel = "Discount (fixed)";
  } else if (data.discount_type === "percentage") {
    const pct = parseFloat(data.discount_value) || 0;
    discountAmt = round2(subtotal * pct / 100);
    discountLabel = `Discount (${pct.toFixed(2)}%)`;
  }

  const taxable = round2(subtotal - discountAmt);
  const taxRate = parseFloat(data.tax_rate) || 0;
  const taxAmt  = round2(taxable * taxRate / 100);
  const total   = round2(taxable + taxAmt);

  const rows = [
    ["Customer",    data.customer_name],
    ["Email",       data.customer_email],
    ["Description", data.description],
    ["Quantity",    data.quantity],
    ["Unit Price",  formatCurrency(data.unit_price, data.currency)],
    ["Subtotal",    formatCurrency(subtotal, data.currency)],
  ];

  if (discountAmt > 0) {
    rows.push([discountLabel, `- ${formatCurrency(discountAmt, data.currency)}`]);
  }

  rows.push(
    [`Tax (${taxRate.toFixed(2)}%)`, formatCurrency(taxAmt, data.currency)],
    ["Total Due",   formatCurrency(total, data.currency)],
    ["Due Date",    data.due_date],
    ["Currency",    data.currency],
  );

  previewGrid.innerHTML = rows.map(([dt, dd], i) => {
    // Highlight total due row
    const isTotalRow = dt === "Total Due";
    const isDiscountRow = dt.startsWith("Discount") && discountAmt > 0;
    const ddClass = isTotalRow ? 'class="preview-total"'
                  : isDiscountRow ? 'class="preview-discount"'
                  : '';
    return `<dt>${dt}</dt><dd ${ddClass}>${escapeHtml(dd)}</dd>`;
  }).join("");

  confirmModal.classList.remove("hidden");
  confirmBtn.focus();
}

function closeConfirmModal() {
  confirmModal.classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Invoice submission
// ---------------------------------------------------------------------------

async function submitInvoice(data) {
  confirmBtn.disabled = true;
  confirmBtn.textContent = "Generating…";

  try {
    const res = await fetch("/api/invoices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    const json = await res.json();

    if (!res.ok) {
      const msg = json.errors ? json.errors.join(" ") : "Failed to create invoice.";
      throw new Error(msg);
    }

    closeConfirmModal();
    showSuccessModal(json.invoice);
    await loadPersistedInvoices();
    form.reset();
    // Reset discount field state after form reset
    onDiscountTypeChange();
    updateTotals();
    clearErrors();

  } catch (err) {
    closeConfirmModal();
    showToast(err.message || "An unexpected error occurred.");
  } finally {
    confirmBtn.disabled = false;
    confirmBtn.textContent = "✓ Generate Invoice PDF";
  }
}

// ---------------------------------------------------------------------------
// Success modal
// ---------------------------------------------------------------------------

function showSuccessModal(invoice) {
  const amount = formatCurrency(invoice.total_due || invoice.subtotal, invoice.currency);
  successMsg.textContent =
    `Invoice ${invoice.invoice_number} for ${invoice.customer_name} ` +
    `(${amount}) has been created.`;

  downloadLink.href = invoice.browser_download_url || `/api/invoices/${invoice.invoice_number}/download`;
  downloadLink.download = `${invoice.invoice_number}.pdf`;

  successModal.classList.remove("hidden");
  closeSuccessBtn.focus();
}

function closeSuccessModal() {
  successModal.classList.add("hidden");
}

/** Minimal HTML escaping to avoid XSS in table cells. */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

previewBtn.addEventListener("click", () => {
  const data = readForm();
  if (validateForm(data)) {
    openConfirmModal(data);
  }
});

cancelBtn.addEventListener("click", closeConfirmModal);

confirmBtn.addEventListener("click", () => {
  const data = readForm();
  submitInvoice(data);
});

closeSuccessBtn.addEventListener("click", closeSuccessModal);

// Close modals on overlay click
confirmModal.addEventListener("click", (e) => {
  if (e.target === confirmModal) closeConfirmModal();
});
successModal.addEventListener("click", (e) => {
  if (e.target === successModal) closeSuccessModal();
});

// Close modals on Escape key
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    closeConfirmModal();
    closeSuccessModal();
  }
});

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
initDueDate();
onDiscountTypeChange();   // set initial disabled state for discount_value


// Invoice history: single renderer, using signed browser PDF links supplied by the API.
async function loadPersistedInvoices() {
  if (!historyBody) return;
  try {
    const res = await fetch("/api/invoices", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const invoices = await res.json();
    if (!Array.isArray(invoices) || !invoices.length) {
      historyBody.innerHTML = '<tr id="emptyRow"><td colspan="6" class="empty-msg">No invoices yet. Create one on the left!</td></tr>';
      return;
    }
    historyBody.innerHTML = invoices.map(inv => {
      const currency = inv.currency || "SGD";
      const amount = formatCurrency(inv.total_due ?? inv.subtotal ?? 0, currency);
      const status = String(inv.status || "unpaid");
      const url = inv.browser_download_url;
      const safeUrl = typeof url === "string" && (/^\/api\/invoices\//.test(url) || /^https:\/\//i.test(url)) ? url : null;
      const pdf = safeUrl
        ? `<a class="dl-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">View PDF</a>`
        : '<span class="pdf-unavailable">PDF unavailable</span>';
      return `<tr>
        <td><span class="inv-number">${escapeHtml(inv.invoice_number || "")}</span></td>
        <td>${escapeHtml(inv.customer_name || "")}</td>
        <td>${escapeHtml(amount)}</td>
        <td>${escapeHtml(inv.due_date || "")}</td>
        <td><span class="status-badge status-${status.replace(/[^a-z_-]/gi, "")}">${escapeHtml(status.replace(/_/g, " "))}</span></td>
        <td>${pdf}</td>
      </tr>`;
    }).join("");
  } catch (err) {
    console.error("Unable to load invoice history:", err);
    historyBody.innerHTML = '<tr><td colspan="6" class="empty-msg">Invoice history is temporarily unavailable.</td></tr>';
  }
}

// Judge sample data only fills the form. It never creates database records.
document.getElementById("fillSampleInvoiceBtn")?.addEventListener("click", () => {
  const sample = {
    customer_name: "Demo Business Pte Ltd",
    customer_email: "demo@example.com",
    description: "Website Maintenance",
    quantity: "2",
    unit_price: "150",
    currency: "SGD",
    discount_type: "none",
    tax_rate: "0"
  };
  Object.entries(sample).forEach(([id, value]) => {
    const field = document.getElementById(id);
    if (field) field.value = value;
  });
  const dueDate = document.getElementById("due_date");
  if (dueDate) {
    const date = new Date();
    date.setDate(date.getDate() + 14);
    dueDate.value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }
  onDiscountTypeChange();
  updateTotals();
  clearErrors();
  form.scrollIntoView({ behavior: "smooth", block: "start" });
  document.getElementById("customer_name")?.focus({ preventScroll: true });
});

loadPersistedInvoices();

// Refresh invoice history automatically after invoices are created via the AI chat.
// This reads existing invoices only; it never creates or changes records.
let invoiceHistoryRefreshRunning = false;
async function refreshInvoiceHistorySafely() {
  if (invoiceHistoryRefreshRunning || document.hidden) return;
  invoiceHistoryRefreshRunning = true;
  try {
    await loadPersistedInvoices();
  } finally {
    invoiceHistoryRefreshRunning = false;
  }
}

// Poll every 10 seconds while the page is visible.
setInterval(refreshInvoiceHistorySafely, 10000);

// Refresh immediately when a visitor returns to this tab.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshInvoiceHistorySafely();
});
