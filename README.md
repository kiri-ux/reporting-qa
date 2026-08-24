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

**The device rule is the one that needs explaining.** The Devices breakout
excludes Mobile Conquesting, PPC, YouTube, LinkedIn and Performance Max.
Comparing it to the whole campaign produces false alarms, so the tool sums only
the line items whose product is eligible and compares against that. Line-item
names carry the product as a suffix (`... Geo-Retargeting Mobile`,
`... AI B2B CTV`), and those names wrap across lines in the PDF, so the parser
rejoins them before testing. Change the exclusion list with
`DEVICE_EXCLUDED_PRODUCTS`.

## Completeness

Import an order-level CSV on `/orders` and the tool also answers:

- **Which reports should have arrived and did not**, with the buyer and P&A team
  member attached so it routes itself.
- **Which lifetime reports are due**, meaning a campaign whose end date falls
  inside the period with no lifetime report in the batch.

Recognised headers, in any order, extra columns ignored:
`Market, Client, Account, Campaign, Start Date, End Date, Buyer,
P&A Team Member, Buyer Email, Team Email`.

Reports match order lines by account number first, then by normalised client
name.

## Deploying to Render

1. Push this repo to GitHub.
2. Render → **Blueprints** → point it at the repo. `render.yaml` creates the web
   service, a Postgres database and a 10 GB disk for the PDFs.
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
