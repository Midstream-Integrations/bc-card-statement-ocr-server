# Card Statement OCR Server for Business Central

![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg) ![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB) ![Business Central 25+](https://img.shields.io/badge/Business%20Central-25%2B-0B7E8A)

**The OCR and receipts-portal server behind [bc-card-statement-ocr](https://github.com/Midstream-Integrations/bc-card-statement-ocr): reads card statements, collects receipts and G/L coding from managers, and writes the result back to the Business Central purchase invoice.**

<!-- DEMO: record a 30-60 second screen capture, save it as docs/images/demo.gif, then replace this comment with:
![Demo](docs/images/demo.gif) -->

## What it does

- **Reads the statement.** BC uploads a card statement PDF; the server pulls out every cardholder and charge. Scanned statements are read with docTR, on the server itself.
- **Receipts portal.** Managers sign in with Microsoft 365 (no BC license), upload a receipt per charge (photo, PDF or iPhone HEIC), and enter the G/L account and dimension codes. Optionally (`OCR_READ_RECEIPTS=true`), each receipt is read and checked against the charge amount and date.
- **Writes back to BC.** On Submit, the server adds the invoice lines with dimensions, attaches the receipts, comments on anything that didn't match, e-signs the Requisition Form, and emails the BC user who sent it that it's approved.
- **Requisition approval.** Vendor invoices sent from BC get an approval link by email; an optional 2nd approver is emailed once the first approves. Delegates can approve on someone's behalf.

## How it fits together

```
Business Central (bc-card-statement-ocr)          this server
  Purchase Invoice ──statement PDF / approval──▶  FastAPI  (/api/*, X-API-Key)
                                                     │
  invoice lines, receipts,  ◀── BC API (Entra app) ──┤
  signed Requisition Form                            │
                                                  Streamlit portal ◀── managers (Microsoft 365 sign-in)
```

| Folder | |
|---|---|
| `CardStatementOCR-Server/api.py` | BC-facing API (FastAPI), behind nginx at `/api/` |
| `CardStatementOCR-Server/portal.py` | Receipts and approvals portal (Streamlit) |
| `CardStatementOCR-Server/cards/` | Statement parsers, receipt checks, BC write-back |
| `CardStatementOCR-Server/reqapproval/` | Requisition approval, 2nd-approver emails |
| `CardStatementOCR-Server/core/` | Settings, database, OCR, BC client, Requisition Form stamping |
| `CardStatementOCR-Server/deploy/` | systemd units and nginx site |

## Install

The full walk-through, covering the server, Microsoft Entra, Business Central and email, is in **[docs/INSTALL.md](docs/INSTALL.md)**. In short:

1. Install the [bc-card-statement-ocr](https://github.com/Midstream-Integrations/bc-card-statement-ocr) extension in Business Central.
2. Register one Entra app (BC API access, portal sign-in, and sending approval emails through Microsoft Graph) and add it to BC's **Microsoft Entra Applications** page. An Exchange admin lets it send as your approvals mailbox only.
3. On an Ubuntu 24.04 or Debian 13 server: Python 3.12 venv, `pip install -r requirements.txt`, fill in `config/ocr.env` and `config/streamlit/secrets.toml`.
4. Start the two systemd services behind nginx with a TLS certificate. Optionally, limit `/api/` to Business Central's addresses.
5. In BC, fill in **Endpoint API Setup** and press **Test OCR Connection**.

## Technical details

| | |
|---|---|
| Runtime | Python 3.12, FastAPI, Streamlit, docTR (CPU), PyMuPDF |
| Server size | 2 vCPU, 4 GB RAM to start; receipt checking (`OCR_READ_RECEIPTS`) needs more |
| Storage | SQLite + files in `OCR_DATA_DIR` |
| Email | Microsoft Graph `sendMail` as the Entra app, scoped to one mailbox (no mailbox password); SMTP for non-Microsoft providers |
| BC API used | Standard API v2.0 plus `/api/midstream/RequisitionApprovalInvoices/v1.0` from the extension |
| Card issuers | Huntington Business Card, U.S. Bank Business Card; detected automatically from the PDF (add more in `cards/parsers.py`) |
| G/L format | `xxxxx-yy-zzzz` (account, dimension 1, dimension 2); see INSTALL for changing it |

## Need help with this?

[Midstream Integrations](https://www.MidstreamIntegrations.com) hosts this server for you, installs and configures it, adds parsers for your card issuer, and supports it.
**[Book a 20-minute call](https://midstreamintegrations.com/contact)** · intro@MidstreamIntegrations.com

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING](https://github.com/Midstream-Integrations/.github/blob/main/CONTRIBUTING.md). New statement parsers are especially welcome.
Found a security problem? Please follow the [security policy](https://github.com/Midstream-Integrations/.github/blob/main/SECURITY.md) instead of opening a public issue.

## License

[GNU Affero General Public License v3.0](LICENSE) © Midstream Integrations

You can use, change and self-host this server for free. If you change it and let other people use it over a network (for example, hosting it for a client), you must offer those people your changed source code under the same license; set `OCR_SOURCE_URL` so the portal's **Source code** link points to it. If that doesn't work for your business, [contact us](https://www.MidstreamIntegrations.com) about a commercial license.

The Business Central extension in [bc-card-statement-ocr](https://github.com/Midstream-Integrations/bc-card-statement-ocr) is MIT licensed.
