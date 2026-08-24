# Report QA

Checks TapClicks digital marketing reports the moment they arrive, so nobody
finds out from the client that a creative table double-counted or a preview
came through blank.

Reports are emailed out automatically on the 1st. Add the tool's inbound
address as a BCC or forwarding rule and every batch gets checked before anyone
opens it.

---

## What it checks

Each rule produces a finding with a severity. **fail** means the report
contradicts itself. **warn** means something a client would notice. **info**
means expected behaviour, recorded so nobody re-raises it.

| Check | Severity | What it catches |
|---|---|---|
| Top-line CTR | fail | Stated CTR does not equal the report's own clicks over impressions |
| CTR excluding CTV | info | Stated CTR matches clicks over *non-CTV* impressions. Expected |
| Line items vs top line | fail | Line-item impressions or clicks do not sum to the headline |
| CTV clicks excluded | info | The click gap is exactly the CTV line items. Expected |
| Creative vs top line | fail | Creative tables claim more than the campaign delivered |
| Creative partial coverage | info | Creative tables cover part of the buy, normal for CTV and Performance Max |
| Device breakout over | fail | Device totals exceed device-eligible impressions. Filtering only removes |
| Device breakout under | warn | Device runs more than 10% under eligible, beyond unknown-device filtering |
| Row maths | warn | A row's CTR does not match its own impressions and clicks |
| Rate ceiling | warn | Any rate printed above 100% |
| Missing thumbnails | warn | The report prints "Thumbnail not available" |
| Blank widget page | warn | A page with a footnote and no chart, table or numbers |
| Geo-fence business name | info | Address rows with no business name. Expected if the fence used an address list |
| Product missing | fail | A product on the client's live orders that is not on the report |
| Product rogue | fail | A product on the report that no qualifying order covers |

**The device rule is the one that needs explaining.** The Devices breakout
excludes Mobile Conquesting, PPC, YouTube, LinkedIn and Performance Max.
Comparing it to the whole campaign produces false alarms, so the tool sums only
the line items whose product is eligible and compares against that. Line-item
names carry the product as a suffix (`... Geo-Retargeting Mobile`,
`... AI B2B CTV`), and those names wrap across lines in the PDF, so the parser
rejoins them before testing. Change the exclusion list with
`DEVICE_EXCLUDED_PRODUCTS`.

## Completeness

The order list is the second half of the tool. Point it at your S3 object and
it also answers:

- **Which reports should have arrived and did not**, with the buyer and P&A team
  member attached so it routes itself.
- **Which lifetime reports are due**, meaning a campaign whose end date falls
  inside the period with no lifetime report in the batch.

Recognised headers, in any order, extra columns ignored:
`Market, Client, Account, Campaign, Start Date, End Date, Buyer,
P&A Team Member, Buyer Email, Team Email`.

Reports match order lines by account number first, then by normalised client
name.

### Products on the report

One client gets one report, and that report should contain exactly the products
their live orders cover. The tool works out what is on the report from section
titles ("Native Display Creative Performance") and line-item name suffixes
("... Geo-Retargeting Mobile" is Mobile Conquesting), then compares that to the
order list.

It deliberately does not search the raw text. Every report carries a footnote
mentioning CTV and TikTok whether or not either is on the buy, so text search
reports both on every client.

The check stays silent for a client that is not on the order list, rather than
guessing. SEO is excluded, since it goes out as its own report.

### Which orders get a report

Point the tool at the IO tool's own export and it recognises the format and
applies the eligibility rules itself:

- **Live IOs, or orders that were live inside the period.** Order status must be
  IO Live or IO Complete.
- **No RFPs**, at either the order or the line-item level.
- **Nothing that ended before the period started.**
- **No cancelled line items.**
- Rolled up to **one row per client and product**, since one client gets one
  report.

The export arrives at daily grain, so a few dozen line items come through as
thousands of rows, and both ids are wrapped in HTML anchors. All of that is
handled. Leave the period blank on import and it uses the previous month.

The import summary reports what it dropped and why, so a client missing from the
expected list can be traced rather than guessed at.

### The order list in S3

Set `ORDERS_S3_BUCKET` and `ORDERS_S3_KEY` and the bucket becomes the source of
truth. CSV or XLSX both work; for a workbook, `ORDERS_S3_SHEET` picks the tab,
or it takes the first one.

Every incoming batch refreshes the list before judging completeness, so a
campaign added or ended since the last run is already reflected. Two things stop
that being wasteful or fragile:

- The object is only re-downloaded when its **ETag changes**, and re-checked at
  most once an hour (`ORDERS_REFRESH_MINUTES`).
- If S3 is unreachable or the file will not parse, **the last good list is kept**
  and the failure is shown on `/orders`. A bad deploy of the spreadsheet cannot
  make the tool forget which reports it expects.

`Sync from S3 now` on `/orders` forces a refresh. Manual upload still works and
is useful for testing a change before it goes in the bucket, but the next batch
re-syncs and overwrites it.

**Yes, credentials are required.** The bucket and key alone are not enough
unless the object is public, which it should not be. Create an IAM user with
read access to that one key and set `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` in the Render environment. boto3 picks those up on its
own, so nothing else needs configuring. The policy needs only `s3:GetObject`
on that key (`HeadObject` is covered by the same permission):

```json
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Action":["s3:GetObject"],
  "Resource":"arn:aws:s3:::YOUR-BUCKET/reporting/order-list.xlsx"}]}
```

## Deploying to Render

1. Push this repo to GitHub.
2. Render → **Blueprints** → point it at the repo. `render.yaml` creates the web
   service, a Postgres database and a 10 GB disk for the PDFs.

   **Use the blueprint, not a manually created web service.** The blueprint wires
   `DATABASE_URL` to the Postgres instance. Without it the app falls back to
   SQLite on the container filesystem, which is wiped on every deploy. If the
   logs say `Running on sqlite, not Postgres`, that is what happened.
3. Set the optional env vars in the dashboard: `SLACK_WEBHOOK_URL`, and the
   `SMTP_*` / `DIGEST_*` values if you want the email digest.
4. Copy `INBOUND_SECRET` from the Render environment tab. Your inbound URL is
   `https://<service>.onrender.com/inbound/mailgun?k=<secret>`.

The Docker image installs `poppler-utils`, which the checks need for
`pdftotext`, `pdfinfo` and `pdftoppm`.

## Wiring up the email

**Mailgun** — add a Route with `match_recipient("reports@qa.yourdomain.com")`
and action `forward("https://<service>.onrender.com/inbound/mailgun?k=<secret>")`.
Mailgun posts a multipart form with the attachments, which is what the endpoint
expects.

**Postmark** — create an inbound stream and set its webhook to
`/inbound/postmark?k=<secret>`. Attachments arrive base64 encoded in the JSON.

Then add that address as a BCC on the TapClicks schedule, or a forwarding rule
in the mailbox that receives them. Zips are unpacked automatically and
`__MACOSX` entries are ignored, so a zipped batch works the same as loose PDFs.

Nothing depends on the email arriving on the 1st. Whenever the data fetch
finishes and the mail goes out, the batch gets checked.

## Notifications

- **Slack** — one message per batch, listing every report that failed or warned
  with its owner. Set `SLACK_WEBHOOK_URL`.
- **Email digest** — an HTML summary to `DIGEST_TO`, plus the buyer and team
  member of any failing report when their email is on the order line.
- **Dashboard** — always on, holds the full detail and the source PDFs.

`Re-send digest and Slack post` on a batch page fires both again.

## Troubleshooting

**`table batches already exists` on boot.** Fixed. Gunicorn starts two workers
and both ran `create_all()`, which checks then creates and is not atomic, so the
loser died. Startup now takes a Postgres advisory lock, and retries on SQLite.

**`Running on sqlite, not Postgres` in the logs.** `DATABASE_URL` is not set.
Deploy from the blueprint, or add the Postgres connection string by hand.

**New columns after an upgrade.** `create_all()` never alters an existing table,
so columns added after the first release are applied on startup from
`ADDITIVE_COLUMNS` in `app/db.py`. Add a row there when you add a column.

## Running locally

```bash
pip install -r requirements.txt
sudo apt-get install poppler-utils
cp .env.example .env
uvicorn app.main:app --reload
```

Then open http://localhost:8000 and drop a batch of PDFs on the dashboard.

## Tests

```bash
python -m pytest tests -q
```

The fixtures under `tests/fixtures/` are real July 2026 reports whose answers
were verified by hand. They pin the behaviour that is easy to regress: that
Benton Rodeo is clean, that Centre Hills' device overage is a failure, that
Watsontown's device table reconciles once Mobile Conquesting is excluded, and
that the CTV click and CTR bases are informational rather than errors.

## Adding a check

Write a function in `app/checks/rules.py` that takes `ctx` and returns a list of
findings, then add it to `RULES`. `ctx` gives you `text`, `tables`, `imps`,
`clicks`, `ctr`, `pages` and `path`. A rule that raises is caught and reported
rather than sinking the batch.
