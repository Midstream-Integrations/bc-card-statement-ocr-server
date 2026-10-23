# Install and configure

This guide sets up the whole solution: the **Business Central extension** ([bc-card-statement-ocr](https://github.com/Midstream-Integrations/bc-card-statement-ocr)), **this server**, one **Microsoft Entra app**, and **email**. Plan on an afternoon the first time. Do it against a BC **sandbox** first, then repeat the BC and `.env` steps for Production.

**Contents**
1. [What you need](#1-what-you-need)
2. [Business Central: install the extension](#2-business-central-install-the-extension)
3. [Microsoft Entra: register the app](#3-microsoft-entra-register-the-app)
4. [Business Central: let the server in](#4-business-central-let-the-server-in)
5. [Server: install](#5-server-install)
6. [Server: configure](#6-server-configure)
7. [Server: HTTPS and services](#7-server-https-and-services)
8. [Business Central: connect to the server](#8-business-central-connect-to-the-server)
9. [Test it end to end](#9-test-it-end-to-end)
10. [Day-to-day operation](#10-day-to-day-operation)
11. [Customizing](#11-customizing)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. What you need

| | |
|---|---|
| Business Central | Online, version 25 or later, and a sandbox to test in |
| Microsoft Entra ID | An admin who can register apps and grant admin consent |
| Server | Ubuntu 24.04 LTS (comes with Python 3.12), or Debian 13 (see [5.1](#51-debian-13-trixie)). Suggested: 2 vCPU and 4 GB RAM; OCR of scanned statements is CPU-heavy. About 10 GB of disk plus room for receipts |
| Host name | A DNS name for the server, for example `ocr.example.com`, reachable from the internet over HTTPS (Business Central Online calls it from Microsoft's cloud, and managers open the portal from anywhere) |
| Mailbox | One mailbox the server sends approval emails from, for example `approvals@example.com` |
| Requisition Form | A BC report that prints your requisition form (see [11.3](#113-requisition-form-requirements)) |

You will collect these values along the way. Keep them somewhere safe:

| Value | Where it comes from | Goes into |
|---|---|---|
| Tenant ID | Entra admin center → Overview | `ocr.env`, `secrets.toml` |
| Client ID and client secret | Entra app registration (step 3) | `ocr.env`, `secrets.toml`, BC Entra Applications |
| BC environment name | BC admin center (`Production`, or your sandbox name) | `ocr.env` |
| API key | You generate it (step 6) | `ocr.env`, BC Endpoint API Setup |
| Company ID | Step 6.4 | `ocr.env` |

---

## 2. Business Central: install the extension

1. Download the `.app` from the latest release of [bc-card-statement-ocr](https://github.com/Midstream-Integrations/bc-card-statement-ocr/releases) (or build it from source; see that repo's README).
2. In BC: **Extension Management** → **Manage** → **Upload Extension** → select the file → **Deploy**.
3. **Sandbox only:** open **Extension Management**, select the extension, choose **Configure**, and turn on **Allow HttpClient Requests**. (Production allows outgoing calls by default.)
4. **Email:** the Card Statement OCR action sends the manager's link from BC. Make sure **Email Accounts** has a default account that can send.

---

## 3. Microsoft Entra: register the app

One app does two jobs: the server uses it to call the Business Central API, and the portal uses it to sign people in.

1. **Entra admin center** → **App registrations** → **New registration**.
   - Name: `Card Statement OCR`
   - Supported account types: **Accounts in this organizational directory only** (single tenant).
   - Redirect URI: platform **Web**, value `https://ocr.example.com/oauth2callback` (your host name; add `:port` if you won't use 443).
   - After the app is created, open **Authentication** and, under the same **Web** platform, **Add URI** `https://businesscentral.dynamics.com/OAuthLanding.htm`, then **Save**. BC's **Grant Consent** button (step 4) signs in through that page and fails with AADSTS50011 without it.
2. Copy the **Application (client) ID** and the **Directory (tenant) ID** from **Overview**.
3. **Certificates & secrets** → **New client secret**. Copy the **Value** now; it isn't shown again. Note the expiry date and put a reminder in your calendar: when it expires, sign-in and the BC write-back both stop.
4. **API permissions** → **Add a permission** → **Dynamics 365 Business Central** → **Application permissions** → **API.ReadWrite.All** → **Add**. Then **Grant admin consent**.
   - The default **Microsoft Graph → User.Read** (delegated) permission covers portal sign-in; leave it.

---

## 4. Business Central: let the server in

The server writes invoice lines, dimensions and attachments as the Entra app, so BC needs to know about it.

1. In BC, search for **Microsoft Entra Applications** → **New**.
2. **Client ID**: the one from step 3. **Description**: `Card Statement OCR server`. **State**: **Enabled**.
3. Under **User Permission Sets**, add:
   - `MID OCR USER`
   - `D365 PURCH DOC, EDIT`

   Leave **Company** blank so the server can work in every company, or pick the one company it should use.
4. Choose **Grant Consent** and sign in as an admin. This needs the `OAuthLanding.htm` redirect URI from step 3. If you already chose **Grant admin consent** in Entra (step 3.4), this is optional.

Repeat this in each environment (sandbox and Production).

---

## 5. Server: install

Run these as a user with `sudo` on a fresh Ubuntu 24.04 server.

```bash
# system packages
sudo apt update
sudo apt install -y python3.12-venv python3-pip nginx certbot python3-certbot-nginx libgl1 libglib2.0-0 git

# a service user with a home folder (docTR caches its models there)
sudo useradd -r -m -d /home/ocr -s /usr/sbin/nologin ocr

# the code
sudo git clone https://github.com/Midstream-Integrations/bc-card-statement-ocr-server.git /srv/bc-card-statement-ocr-server
sudo ln -s /srv/bc-card-statement-ocr-server/CardStatementOCR-Server /opt/ocr
sudo chown -R ocr:ocr /srv/bc-card-statement-ocr-server

# data folder
sudo mkdir -p /var/lib/ocr && sudo chown ocr:ocr /var/lib/ocr && sudo chmod 750 /var/lib/ocr

# Python environment (CPU-only PyTorch first, then everything else)
cd /opt/ocr
sudo -u ocr python3.12 -m venv .venv
sudo -u ocr .venv/bin/pip install --upgrade pip
sudo -u ocr .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
sudo -u ocr .venv/bin/pip install -r requirements.txt
```

The first statement or receipt that's read downloads docTR's models (a few hundred MB) into `/home/ocr`, so the server needs outbound internet access at least once.

### 5.1 Debian 13 (trixie)

Debian 13 ships Python 3.13 and has no `python3.12-venv` package. The server is pinned and tested on Python 3.12, so on Debian you get 3.12 from [uv](https://docs.astral.sh/uv/) instead. uv installs its own copy of Python 3.12 in `/home/ocr` and leaves the system Python alone. Everything after this section (settings, nginx, services, cron) is the same as on Ubuntu.

Cloud images such as Linode's log you in as `root` and may not have `sudo`. Install it first, or run the commands as `root` without the `sudo` prefix:

```bash
apt update && apt install -y sudo
```

Then:

```bash
# system packages (curl for the uv installer; libglib2.0-0t64 is Debian 13's name for libglib2.0-0)
sudo apt update
sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx libgl1 libglib2.0-0t64 git curl

# uv, installed for all users
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh

# a service user with a home folder (docTR caches its models there; uv keeps Python 3.12 there)
sudo useradd -r -m -d /home/ocr -s /usr/sbin/nologin ocr

# the code
sudo git clone https://github.com/Midstream-Integrations/bc-card-statement-ocr-server.git /srv/bc-card-statement-ocr-server
sudo ln -s /srv/bc-card-statement-ocr-server/CardStatementOCR-Server /opt/ocr
sudo chown -R ocr:ocr /srv/bc-card-statement-ocr-server

# data folder
sudo mkdir -p /var/lib/ocr && sudo chown ocr:ocr /var/lib/ocr && sudo chmod 750 /var/lib/ocr

# Python 3.12 environment (--seed adds pip to the venv)
cd /opt/ocr
sudo -u ocr uv venv --seed --python 3.12 .venv
sudo -u ocr .venv/bin/python --version     # should print Python 3.12.x

# Debian 13 mounts /tmp in RAM, which is too small for the torch download, so pip uses a folder on disk
sudo -u ocr mkdir -p /home/ocr/tmp
sudo -u ocr env TMPDIR=/home/ocr/tmp .venv/bin/pip install --upgrade pip
sudo -u ocr env TMPDIR=/home/ocr/tmp .venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
sudo -u ocr env TMPDIR=/home/ocr/tmp .venv/bin/pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
sudo -u ocr .venv/bin/pip show torch | head -2   # version should end in +cpu
```

If pip fails with `[Errno 28] No space left on device` while `df -h /` shows free space, `TMPDIR` wasn't set: `/tmp` filled up, not the disk.

Don't delete `/home/ocr/.local/share/uv`: the venv's Python lives there.

---

## 6. Server: configure

All settings live in `/opt/ocr/config/`. Both files hold secrets, so they're excluded from git and should be readable only by the `ocr` user.

### 6.1 `config/ocr.env`

```bash
cd /opt/ocr
sudo -u ocr cp config/ocr.env.example config/ocr.env
sudo chmod 600 config/ocr.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"     # -> OCR_API_KEY
sudo -u ocr nano config/ocr.env
```

| Setting | What to put |
|---|---|
| `OCR_DATA_DIR` | `/var/lib/ocr` (database, statements, receipts) |
| `OCR_PUBLIC_URL` | `https://ocr.example.com`, the address in emailed links |
| `OCR_API_KEY` | The key you just generated. BC sends it with every call |
| `OCR_ADMINS` | Comma-separated emails of people who also see the **Statement breakdown** page |
| `OCR_TIMEZONE` | Time zone printed in Requisition Form signatures, for example `America/New_York` |
| `OCR_RECEIPT_DATE_WINDOW` | How many days a receipt date may be from the charge date before it's flagged (default 3) |
| `OCR_READ_RECEIPTS` | `true` to read each receipt and check it against the charge amount and date; `false` (default) to just attach it. Reading needs a reasonably fast server |
| `OCR_TIMEOUT_SECONDS` | How long to spend reading a receipt before giving up (default 5), when `OCR_READ_RECEIPTS` is on. The file is still attached and the user is asked to explain it. Raise it if good receipts are being cut off on a slow server |
| `BC_TENANT_ID`, `BC_CLIENT_ID`, `BC_CLIENT_SECRET` | From step 3 |
| `BC_ENVIRONMENT` | `Production`, or the sandbox name exactly as the BC admin center shows it |
| `BC_DIM1_CODE`, `BC_DIM2_CODE` | The codes in BC **General Ledger Setup** → **Shortcut Dimension 1 Code** / **2 Code** |
| `REQAPPROVAL_COMPANY_ID` | See 6.4 |
| `SMTP_*` | See 6.3 |
| `OCR_AP_EMAIL`, `OCR_IT_EMAIL` | Optional contacts shown at the bottom of approval emails |
| `OCR_DELEGATES` | Optional; see [10.3](#103-delegates) |
| `OCR_SOURCE_URL` | Optional. The portal's **Source code** link. Leave it unless you've changed the code; then point it at your copy (the AGPL license requires this) |

Never set `OCR_DEV_USER` on a server: it skips sign-in.

### 6.2 `config/streamlit/secrets.toml` (portal sign-in)

```bash
sudo -u ocr cp config/streamlit/secrets.toml.example config/streamlit/secrets.toml
sudo chmod 600 config/streamlit/secrets.toml
python3 -c "import secrets; print(secrets.token_urlsafe(48))"     # -> cookie_secret
sudo -u ocr nano config/streamlit/secrets.toml
```

- `redirect_uri`: exactly the redirect URI from step 3, for example `https://ocr.example.com/oauth2callback`.
- `cookie_secret`: the value you just generated.
- `client_id`, `client_secret`: from step 3.
- `server_metadata_url`: replace `YOUR-TENANT-ID` with your tenant ID. This keeps sign-in to your own organization.

`.streamlit` in the code folder is a link to `config/streamlit`, so Streamlit finds these files when it starts in `/opt/ocr`.

### 6.3 Email

The server sends the requisition-approval emails (first and second approver), and an "Approved" email to the BC user who sent a card statement or requisition once it's approved, over SMTP with STARTTLS. The card statement email to the manager goes from BC itself (step 2.4).

- **Microsoft 365:** `SMTP_SERVER=smtp.office365.com`, `SMTP_PORT=587`, `SMTP_USER` = the mailbox address, `SMTP_PASS` = its password. In the Exchange admin center, open the mailbox → **Manage email apps** → turn on **Authenticated SMTP**. The mailbox can't require MFA for this sign-in.
  Microsoft is retiring password (Basic) authentication for SMTP. It keeps working through December 2026; after that it's turned off by default for existing tenants, and an Exchange admin has to turn it back on. If your tenant has it disabled, use another SMTP provider.
- **Any other provider** (SendGrid, Amazon SES, your own relay) works if it supports STARTTLS on the port you set and a user name and password.

### 6.4 Company ID and a first connection test

`REQAPPROVAL_COMPANY_ID` is the BC company's SystemId, used by the 2nd-approver sweep. This command lists the companies, and also proves the Entra app and BC permissions work:

```bash
cd /opt/ocr
sudo -u ocr .venv/bin/python -c "
import requests
from core.bc_client import _token, API_ROOT
from core.settings import BC_TENANT_ID as t, BC_ENVIRONMENT as e
r = requests.get(f'{API_ROOT}/{t}/{e}/api/v2.0/companies', headers={'Authorization': 'Bearer ' + _token()}, timeout=60)
r.raise_for_status()
for c in r.json()['value']: print(c['id'], c['name'])
"
```

Copy the `id` of your company into `REQAPPROVAL_COMPANY_ID`. If this fails, see [Troubleshooting](#12-troubleshooting) before going further.

---

## 7. Server: HTTPS and services

### 7.1 Certificate

Point your DNS name at the server and open ports 80 and 443 (`sudo ufw allow 'Nginx Full'` if you use ufw). Then:

```bash
sudo certbot certonly --nginx -d ocr.example.com
```

Certbot renews automatically. If port 80 can't be opened, use a DNS challenge instead (`certbot certonly --manual --preferred-challenges dns`, or your DNS provider's certbot plugin).

### 7.2 nginx

```bash
sudo cp /opt/ocr/deploy/nginx-ocr.conf /etc/nginx/sites-available/ocr
sudo sed -i 's/ocr.example.com/YOUR-HOST/g' /etc/nginx/sites-available/ocr
sudo ln -s /etc/nginx/sites-available/ocr /etc/nginx/sites-enabled/ocr
sudo nginx -t && sudo systemctl reload nginx
```

nginx sends `/api/` to the BC-facing API (port 8010) and everything else to the portal (port 8501). Both only listen on `127.0.0.1`. Uploads are limited to 30 MB per request by `client_max_body_size`.

To use a port other than 443, change `listen 443 ssl;` and add the port to `OCR_PUBLIC_URL`, the Entra redirect URI and `redirect_uri`.

### 7.3 Services

```bash
sudo cp /opt/ocr/deploy/ocr-api.service /opt/ocr/deploy/ocr-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-api ocr-portal
systemctl status ocr-api ocr-portal
```

Quick checks:

```bash
curl https://ocr.example.com/api/health                                   # {"ok":true}
curl -H "X-API-Key: YOUR-KEY" https://ocr.example.com/api/merchants       # the card issuers this server reads
```

Open `https://ocr.example.com` in a browser and sign in with a work account.

### 7.4 2nd-approver sweep (cron)

Second approvers are normally emailed the moment the first approver submits. This job catches anything that didn't go out (for example a mail outage). Run it every 15 minutes. Debian cloud images may not include cron, so install it first there (`sudo apt install -y cron`):

```bash
sudo crontab -u ocr -e
```

```
*/15 * * * * cd /opt/ocr && .venv/bin/python -m reqapproval.secondary 2>&1 | logger -t ocr-secondary
```

### 7.5 Optional hardening: only Business Central can call the API

Port 443 serves two audiences: Business Central calls `/api/`, and managers open the portal from wherever they are. So 443 has to stay open to the internet, but `/api/` can be limited to Business Central's addresses. The portal keeps working from anywhere and still requires Microsoft 365 sign-in, and the API still requires the API key; this adds a network check in front of it.

Microsoft publishes Business Central's outgoing addresses as the `Dynamics365BusinessCentral` service tag, in the weekly [Azure IP Ranges and Service Tags - Public](https://www.microsoft.com/en-us/download/details.aspx?id=56519) file. `deploy/update-bc-allowlist.py` downloads that file and writes the tag's ranges to `/etc/nginx/ocr-bc-allow.conf` as an nginx allow list. It only uses the Python standard library, so it runs with the system `python3`.

Do this after section 7.3 works.

1. **Optional: extra addresses.** To call `/api/` from your office (for example the `curl` checks in 7.3), list those IPs or CIDRs, one per line, in `/etc/nginx/ocr-bc-allow.local`. Requests from the server itself (`127.0.0.1`) are always allowed.

2. **Build the list.** Look at it first, then apply it:

   ```bash
   sudo python3 /opt/ocr/deploy/update-bc-allowlist.py --dry-run | head
   sudo python3 /opt/ocr/deploy/update-bc-allowlist.py
   ```

3. **Turn it on in nginx.** Uncomment the `include` line in the `/api/` block:

   ```bash
   sudo sed -i 's|# include /etc/nginx/ocr-bc-allow.conf;|include /etc/nginx/ocr-bc-allow.conf;|' /etc/nginx/sites-available/ocr
   sudo nginx -t && sudo systemctl reload nginx
   ```

4. **Check it.**
   - From the server: `curl --resolve <your-host>:443:127.0.0.1 https://<your-host>/api/health` returns `{"ok":true}`.
   - From your own computer (not in the `.local` file): `https://<your-host>/api/health` returns **403 Forbidden**, and the portal still opens.
   - In BC: **Endpoint API Setup** → **Test OCR Connection** still lists the card issuers.

5. **Keep it current.** Microsoft updates the file weekly, and new ranges aren't used for at least a week, so a weekly run is enough. As root (`sudo crontab -e`):

   ```
   17 4 * * 1 /usr/bin/python3 /opt/ocr/deploy/update-bc-allowlist.py 2>&1 | logger -t ocr-bc-allowlist
   ```

   See the result with `journalctl -t ocr-bc-allowlist`.

The script is careful not to lock Business Central out: if the download fails, the tag is missing or has very few ranges, or `nginx -t` fails, it keeps the current list and exits with an error. If Microsoft moves the download link, download the JSON by hand and run the script with `--file ServiceTags_Public_<date>.json`.

Things to know:

- **IPv4 only.** Microsoft's file currently lists only IPv4 ranges. If your host name has an AAAA (IPv6) record and Business Central reaches the server over IPv6, it's blocked. Leave out the AAAA record, or remove it if **Test OCR Connection** fails with this turned on.
- **No proxy in front.** nginx checks the address the request comes from. Behind Cloudflare's proxy (orange cloud) or another load balancer, every request comes from the proxy and is blocked. Keep the DNS record **DNS only**.
- **To turn it off,** comment out the `include` line again and reload nginx.
- **403 in BC.** If BC calls start failing with 403, run the script by hand to pick up new ranges, then check `/var/log/nginx/access.log` for the address that was refused.

---

## 8. Business Central: connect to the server

1. Search for **Endpoint API Setup**.
   - **OCR Host**: `ocr.example.com` (`https://` is added for you; include `:port` if not 443).
   - **OCR API Key**: the `OCR_API_KEY` value. It's stored in isolated storage, and the field clears after you enter it; **OCR API Key Set** shows it's there.
   - **Requisition Form Report ID**: `50120` (**MID Requisition Form**, included in the extension), or your own report (see [11.3](#113-requisition-form-requirements)).
2. Choose **Test OCR Connection**. You should see the list of card issuers.
3. **Requisition Managers**: add every approver's name and email. These people sign in to the portal with that email address.
4. **Permission sets**: assign `MID OCR USER` to everyone who sends statements or requisitions, and `MID ENDPOINT SETUP` to whoever maintains the setup page.

---

## 9. Test it end to end

Use the sandbox and a real (or test) statement.

1. **Card statement.** Create a purchase invoice for the card issuer as vendor → **Send to OCR** → **Card Statement OCR**. Pick the issuer and a manager (optionally a 2nd approver), upload the statement PDF, pick a cardholder.
   - Expect: the statement and a blank `RequisitionForm-<invoice no.>` are attached, and the manager gets an email.
2. **Receipts portal.** As the manager, open the link and sign in. For each charge, upload a receipt and enter the G/L string (for example `60100-01-2000`), then **Submit**.
   - Expect: in BC, one line per charge with dimensions, the receipts attached, a `RequisitionForm-<no.>-signed.pdf`, and **Mgr. Requisition Approval** = `APPROVED`. You (the sender) get an "Approved" email. With `OCR_READ_RECEIPTS=true`, receipts that don't match the charge also get a comment on their line.
3. **Requisition approval.** On another purchase invoice, choose **Send for Requisition Approval**, pick a manager and a 2nd approver, upload a vendor invoice PDF.
   - Expect: the manager's email; after they submit, the 2nd approver's email; after they approve, **2nd Requisition Approval** = `APPROVED` and the form shows both signatures.

When everything passes, repeat steps 2, 4 and 8 in Production, and change `BC_ENVIRONMENT` to `Production`. If you tested against the sandbox, `purge_sandbox.py` removes portal data that belongs to sandbox invoices (see 10.2).

---

## 10. Day-to-day operation

### 10.1 Logs

```bash
journalctl -u ocr-api -u ocr-portal -f          # live
journalctl -t ocr-secondary --since today       # 2nd-approver sweep
```

### 10.2 Backups and data retention

Everything the server keeps is in `OCR_DATA_DIR` (`ocr.db`, `statements/`, `receipts/`) plus `config/`. Back both up daily, for example:

```bash
sqlite3 /var/lib/ocr/ocr.db ".backup '/backup/ocr-$(date +%F).db'"
rsync -a /var/lib/ocr/statements /var/lib/ocr/receipts /opt/ocr/config /backup/
```

Receipts are also attached to the BC invoice, so BC remains the system of record. Decide how long to keep files on the server and delete older ones on a schedule if your policy requires it.

To clear out sandbox test data after switching to Production:

```bash
cd /opt/ocr
sudo -u ocr .venv/bin/python purge_sandbox.py            # dry run
sudo -u ocr .venv/bin/python purge_sandbox.py --apply    # backs up the database, then removes it
```

### 10.3 Delegates

A delegate can open someone else's links and approve for them. The Requisition Form then reads "digitally signed by <delegate> on behalf of <name>". In `ocr.env`, separate entries with `;`:

```
OCR_DELEGATES=assistant@example.com=director@example.com=Pat Director;backup@example.com=cfo@example.com=Sam CFO
```

Restart both services after changing `ocr.env`.

### 10.4 Updating

```bash
cd /srv/bc-card-statement-ocr-server
sudo -u ocr git pull
sudo -u ocr /opt/ocr/.venv/bin/pip install -r /opt/ocr/requirements.txt
sudo systemctl restart ocr-api ocr-portal
```

On Debian 13, run the `pip` line as in 5.1: `sudo -u ocr env TMPDIR=/home/ocr/tmp /opt/ocr/.venv/bin/pip install -r /opt/ocr/requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu`. The `pip` step is only needed when `requirements.txt` changed, but it's harmless to run every time.

Read the CHANGELOG first. If a release changes the BC contract, update the extension at the same time.

### 10.5 Rotating secrets

- **Entra client secret:** create a new one, put it in `ocr.env` and `secrets.toml`, restart both services, then delete the old one.
- **API key:** generate a new one, put it in `ocr.env`, restart, and enter it on **Endpoint API Setup**. Calls fail between those two steps, so do it outside working hours.

---

## 11. Customizing

### 11.1 Add a card issuer

1. In `cards/parsers.py`, write `parse_<issuer>()` and `<issuer>_looks_like()` and register them in `MERCHANTS` (the id is lowercase, no spaces).
2. In the extension, add a value to the **MID Card Merchant** enum and return the same id from `MerchantId()` in `CardStmtMgt.Codeunit.al`.
3. Restart the services; **Test OCR Connection** should list it.

### 11.2 G/L string format

The portal expects G/L strings as `xxxxx-yy-zzzz`: a 5-digit account, then the Shortcut Dimension 1 code (2 digits) and Shortcut Dimension 2 code (4 digits). If your chart of accounts is different, change `GL_FORMAT` in `cards/bc_submit.py` and `reqapproval/bc.py`, and the matching hints and input lengths in `cards/pages.py` and `reqapproval/page.py`.

### 11.3 Requisition Form requirements

The extension includes a generic one-page form, report **50120 MID Requisition Form**, that already meets these requirements: company name, address and logo from **Company Information**, the invoice, vendor and dates, then the Description, G/L Account, Cost and signature lines. Use your own report only if you need a different design. The logo comes from **Company Information** → **Picture**. Use a PNG or JPG with a white (not transparent) background: BC's PDF renderer turns transparency into a color mask that can erase black parts of the logo.

The server signs the form by finding text labels on page 1 of the PDF and writing to the right of them. Your report must print these labels as real text (not part of an image). If one is missing, the manager's Submit stops with an error naming it:

| Label | Filled with |
|---|---|
| `Cost:` | The total |
| `G/L Account:` | The G/L string, or `MULTIPLE` with the split listed beside it |
| `Description:` | The approver's explanation, on the underlines between it and `G/L Account:` (if any) |
| `Approved By:` | The approver's e-signature |
| `Signed By:` | The first approver's signature when a 2nd approver signs `Approved By:` |

BC attaches the form as `RequisitionForm-<invoice no.>`; the signed copy is added as `RequisitionForm-<invoice no.>-signed.pdf` and the blank stays on the invoice.

---

## 12. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| **Test OCR Connection**: "Could not reach the OCR server" | DNS, firewall or certificate. From outside your network, `curl https://<host>/api/health` must work with a valid certificate. In a sandbox, check **Allow HttpClient Requests** (step 2.3). |
| BC shows "Invalid API key" (401) | **OCR API Key** on Endpoint API Setup doesn't match `OCR_API_KEY`. Re-enter it, and restart the services if you changed `ocr.env`. |
| "Failed to obtain BC token" | Wrong tenant ID, client ID or secret, or the secret expired (step 3). |
| BC calls fail with 403 Forbidden after 7.5 | The caller's address isn't in the allow list. Run `update-bc-allowlist.py` by hand, check for an AAAA record or a proxy in front (7.5), and look in `/var/log/nginx/access.log` for the refused address. |
| BC API returns 401/403 | The app isn't on **Microsoft Entra Applications**, isn't **Enabled**, consent wasn't granted, or the permission sets are missing (step 4). Also check `BC_ENVIRONMENT` is spelled exactly as in the admin center. |
| 404 on `RequisitionApprovalInvoices` | The extension isn't installed in that environment, or `BC_ENVIRONMENT` points at a different one. |
| "Dimension '…' doesn't exist in BC" | `BC_DIM1_CODE` / `BC_DIM2_CODE` don't match **General Ledger Setup**. |
| "RequisitionForm-… isn't attached to the invoice" | No **Requisition Form Report ID** on Endpoint API Setup, or the report failed to print. |
| Submit fails with "'Cost:' not found on the requisition form" (or another label) | That label from [11.3](#113-requisition-form-requirements) isn't on the form as text. Fix the report, delete the blank `RequisitionForm-<no.>` from that invoice, choose **Attach As PDF** to print a new one, then have the manager submit again (nothing already added is added twice). |
| No signed form after Submit | The blank `RequisitionForm-<no.>` wasn't on the invoice when the manager submitted, so signing was skipped (see the `ocr-portal` log). Check **Requisition Form Report ID**. |
| Sign-in error AADSTS50011 (redirect URI mismatch) | On the portal: the Entra redirect URI and `redirect_uri` in `secrets.toml` must match exactly, including any port. On BC's **Grant Consent** (the error names `businesscentral.dynamics.com/OAuthLanding.htm`): add that URI to the app's **Authentication** page (step 3). |
| Approval email fails with an authentication error | Authenticated SMTP is off for the mailbox, the password changed, MFA is enforced, or Basic auth for SMTP is disabled in your tenant (6.3). |
| Card statement email not sent | BC's default **Email Account** isn't set up; the error in BC includes the portal link so you can send it by hand. |
| First statement takes minutes | docTR is downloading its models; later runs are faster. BC waits up to 5 minutes. |
| Portal shows nothing for a manager | They must sign in with the same email as on **Requisition Managers** (or be set up as a delegate). |

Still stuck? Open an issue with the log lines from `journalctl -u ocr-api -u ocr-portal` (remove anything confidential), or [contact Midstream Integrations](https://www.MidstreamIntegrations.com).
