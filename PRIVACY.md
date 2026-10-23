# Privacy

This server runs on infrastructure you control (or that your hosting provider runs for you).

It stores card statement PDFs, the transactions read from them, uploaded receipts, approver names and email addresses, and an activity record in `OCR_DATA_DIR` (a SQLite database plus files). It reads and writes purchase invoices, invoice lines, dimensions and document attachments in your Business Central tenant through the Entra application you register, and sends approval emails as the mailbox you configure (through Microsoft Graph with the same Entra application, or through the SMTP server you configure).

Receipts and statements are read on the server itself (docTR); nothing is sent to an outside OCR service. Users sign in with Microsoft Entra ID.

Midstream Integrations does not collect telemetry or any data from this software. You decide how long data is kept; see "Backups and data retention" in [docs/INSTALL.md](docs/INSTALL.md).
