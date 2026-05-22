# Allocations email-to-webhook setup

Automates the Allocations file upload. CloudMailin receives the
allocations email forwarded from Outlook, extracts the .xlsx attachment,
and POSTs it to `/api/allocations/ingest` on the dashboard.

## One-time setup

### 1. Generate an ingest key

Pick a strong random string. Mac/Linux:

```sh
openssl rand -hex 24
```

You'll need this in two places: Vercel (server-side check) and CloudMailin
(custom header on the webhook). Treat it like a password.

### 2. Add the env var on Vercel

Project → Settings → Environment Variables → Add:

- **Name:** `ALLOCATIONS_INGEST_KEY`
- **Value:** the string from step 1
- **Environments:** Production (and Preview if you want)

**Redeploy** so the env var takes effect (a Git push, or the "Redeploy"
button on the latest deployment, will do it).

### 3. Sign up at CloudMailin

<https://www.cloudmailin.com> — free tier covers 200 emails/month, which
is more than enough for twice-daily allocations (~60/month).

After signing up:

- Create an **address**. You'll be assigned something like
  `xxxxxxxx@cloudmailin.net`.
- Configure the address:
  - **HTTP POST format:** `Multipart Normalized` (sends attachments as
    standard multipart form-data files — the format the endpoint expects).
  - **POST target URL:** `https://ducati-forecast.vercel.app/api/allocations/ingest`
  - **Custom headers:** add `X-API-Key: <the-string-from-step-1>`

### 4. Outlook forwarding rule

In Outlook (web or desktop):

- Home → Rules → Manage Rules
- New rule. Conditions:
  - `From contains <the email address Ducati sends allocations from>`
  - and `Subject contains "Allocations"` (or whatever the actual subject pattern is)
  - and `with an attachment` (optional but useful)
- Action: **Forward to** `xxxxxxxx@cloudmailin.net` (the CloudMailin
  address from step 3).
  - If Outlook offers it, **Redirect** is preferable to **Forward** —
    redirected emails preserve the original headers and attachments
    more cleanly. Both usually work for CloudMailin's parser.

## How it behaves

For each forwarded email, CloudMailin POSTs to the ingest endpoint. The
endpoint:

1. Verifies `X-API-Key`. If wrong, returns `401`.
2. Finds the first `.xlsx` attachment.
3. Parses the report date from the filename (pattern:
   `Allocations DD.MM.YY (am|pm).xlsx`).
4. Refuses the upload if the report date is older than the current
   snapshot (returns `200 rejected_older` — CloudMailin won't retry).
5. Otherwise: parses the rows, deduplicates by order number against
   existing allocations, inserts the new ones, logs a row in
   `allocation_reports`.

Response is JSON; CloudMailin shows it in their webhook log so you can
audit each delivery.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 unauthorized` | Custom header missing or doesn't match the env var. Re-check both. |
| `400 no_attachment` | Email didn't include an .xlsx (forwarded as text? attached as link?). Check the Outlook rule. |
| `400 bad_date` | Filename doesn't match the convention. Either rename upstream, or pass a `report_date` form field. |
| `200 rejected_older` | A later-dated allocations file is already in the DB. Expected behaviour. |
| Nothing arrives at CloudMailin | Outlook rule didn't fire — sender/subject filter wrong, or attachment-handling option was "Send-as-link" rather than the file itself. |

## Manual test

You can sanity-check the endpoint from your laptop without touching
CloudMailin:

```sh
curl -X POST https://ducati-forecast.vercel.app/api/allocations/ingest \
  -H "X-API-Key: <your-ingest-key>" \
  -F "attachments[0]=@/path/to/Allocations 21.05.26 am.xlsx"
```

Response should be JSON with `status: ok` (or `rejected_older` if the
current snapshot is already newer).
