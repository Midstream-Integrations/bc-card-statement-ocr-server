# Changelog

All notable changes to this project are listed here.

## [1.0.0] - Unreleased

First public release.

- **Card statements:** BC uploads the statement PDF and the server lists its cardholders and charges. The issuer is recognized from the PDF; parsers for Huntington and U.S. Bank business cards are included. PDFs are read from their text layer first, and docTR (on the server) is only used for scanned pages.
- **Receipts portal:** managers sign in with Microsoft 365, upload a receipt per charge (photo, PDF or HEIC) and enter the G/L string. Charges coded to more than one G/L are listed on the Requisition Form with a total per G/L.
- **Receipt checking (optional):** with `OCR_READ_RECEIPTS=true`, each receipt is read and compared with the charge amount and date. Reading runs in a separate worker and stops after `OCR_TIMEOUT_SECONDS` (default 5); a receipt that times out is still attached and the user is asked to explain it. Off by default, so receipts are simply attached.
- **Write-back to BC:** invoice lines with dimensions, receipts as attachments, comments on anything that didn't match, and an e-signed `RequisitionForm-<no.>-signed.pdf`.
- **Requisition approval:** email link to the manager, optional 2nd approver, delegates, and an "Approved" email to the BC user who sent the statement or requisition.
- **Deployment:** systemd units, an nginx site, and an optional allow list that limits `/api/` to Business Central's published addresses (`deploy/update-bc-allowlist.py`). Install guide for Ubuntu 24.04 and Debian 13.
