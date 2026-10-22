# AI Finance Admin Agent

A beginner-friendly prototype web application that automates invoice generation for small businesses. Built with Python Flask, ReportLab, and vanilla HTML/CSS/JS.

> **Hackathon project — Problem Statement 2: ONE LESS THING**
> Designed to later connect to IBM watsonx Orchestrate for AI-powered workflows.

---

## Features

- **Invoice creation form** — customer name, email, service, quantity, unit price, due date, currency
- **Live subtotal calculation** — updates as you type
- **Confirmation preview** — review before generating the PDF
- **Professional PDF invoices** — generated server-side with ReportLab
- **Invoice history** — persisted in a local SQLite database
- **Input validation** — both client-side and server-side
- **REST API** — `/api/invoices` endpoint ready for watsonx Orchestrate integration

---

## Project Structure

```
ai-finance-admin/
├── app.py                     # Flask backend (routes, DB, PDF generation)
├── requirements.txt           # Python dependencies
├── README.md                  # This file
├── invoices.db                # SQLite database (auto-created on first run)
├── templates/
│   └── index.html             # Dashboard page
├── static/
│   ├── css/
│   │   └── style.css          # Stylesheet
│   └── js/
│       └── main.js            # Frontend logic
└── generated_invoices/        # Generated PDF files (auto-created on first run)
```

---

## Prerequisites

- Python 3.10 or newer
- pip (comes with Python)

No paid APIs, credit cards, or external services are required.

---

## Installation

### 1. Clone or download the project

```bash
git clone <your-repo-url>
cd ai-finance-admin
```

Or simply copy the project folder to your machine.

### 2. Create a virtual environment (recommended)

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Running the Application

```bash
python app.py
```

Open your browser and go to: **http://127.0.0.1:5000**

The SQLite database (`invoices.db`) and the `generated_invoices/` folder are created automatically on the first run.

---

## How to Test

1. **Fill in the form** — enter a customer name, email, service description, quantity, unit price, and due date.
2. **Watch the subtotal** update in real-time as you type.
3. Click **Preview & Confirm** — a modal shows all invoice details for review.
4. Click **Generate Invoice PDF** — the invoice is saved to the database and a PDF is created.
5. Click **↓ Download PDF** in the success popup or in the Invoice History table to download the file.
6. Try submitting the form with missing or invalid values to see validation in action.

---

## API Endpoints

These endpoints are designed for watsonx Orchestrate to call later.

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/api/invoices` | List all invoices (JSON) |
| `POST` | `/api/invoices` | Create a new invoice |
| `GET`  | `/api/invoices/<invoice_number>/download` | Download a PDF |

### POST `/api/invoices` — Request body

```json
{
  "customer_name":  "Acme Pte Ltd",
  "customer_email": "billing@acme.com",
  "description":    "Web Development — Phase 1",
  "quantity":       5,
  "unit_price":     "250.00",
  "due_date":       "2025-08-01",
  "currency":       "SGD"
}
```

### Response

```json
{
  "success": true,
  "invoice": {
    "invoice_number": "INV-20250601-A3F2",
    "customer_name":  "Acme Pte Ltd",
    "subtotal":       "1250.00",
    "currency":       "SGD",
    "status":         "unpaid",
    ...
  }
}
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | `dev-only-change-in-prod` | Flask secret key — change this in production |

Set it before running:

```bash
# Windows PowerShell
$env:SECRET_KEY = "your-secure-random-string"

# macOS / Linux
export SECRET_KEY="your-secure-random-string"
```

---

## What Is Not Implemented Yet

- Payment verification (planned: connect bank feed or payment gateway)
- Email sending (planned: send invoice PDF to customer via email)
- IBM watsonx Orchestrate API integration (planned: trigger workflows from this app)
- Authentication / user accounts
- Multi-user support

---

## Roadmap to watsonx Orchestrate Integration

1. Deploy this Flask app to IBM Code Engine or any cloud host.
2. Expose `/api/invoices` (POST) as an OpenAPI skill in watsonx Orchestrate.
3. Add a `/api/invoices/<id>/mark-paid` endpoint for payment verification.
4. Add a `/api/invoices/<id>/send-reminder` endpoint for email reminders.
5. Build an Orchestrate agent that calls these skills automatically on a schedule.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3, Flask 3 |
| PDF generation | ReportLab |
| Database | SQLite (built into Python) |
| Frontend | HTML5, CSS3, Vanilla JavaScript |
| Deployment target | IBM Code Engine (future) |

---

## License

MIT — free to use, modify, and distribute for the hackathon and beyond.
