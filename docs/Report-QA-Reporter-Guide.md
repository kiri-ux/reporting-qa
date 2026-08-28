# Report QA - reporter's guide

**Tool:** https://qa.reporting.zone
**Who this is for:** anyone who pulls, checks, signs off and packages the monthly reports.

Report QA reads every TapClicks "Digital Marketing Report" PDF as it arrives, checks it against the order export, and tells you what is wrong with it before a partner sees it. It also tracks the whole month: what is owed, what has landed, what is signed off, and what has been packaged and sent.

It does not email anyone. Nothing leaves the tool unless you press a button.

---

## Before you start

**1. Get in.** One shared password, no accounts. Enter it once and the browser remembers you for 30 days.

**2. Set your name.** Top right of the bar, type your name and press **Save**. This is not a login and it does not unlock anything - it is how sign-offs get attributed. Set it once per browser. Two things stop working if you skip it: your name has to be typed on every sign-off, and ticking off the last finding on a report will not auto-review it.

**3. Find your partners.** On the board, open the **Filters** row, pick yourself under **Reporter**, then type a name in **Save these filters as...** and press **Save view**. That view is one click from then on. Views deliberately do not remember the period, so a view saved in July opens on whatever cycle you are looking at now.

**Nav is the dark rail down the left side.** Icons only, hover for the label. The ones you will use: **Reporting Cycle** (the board), **Packaged Links**, **Check a list**, **Lifetimes delivered**, **Batch history**, **Order list**. The brain icon at the top right opens **Reporting rules**, which has two tabs: **What is owed** (when a monthly or a lifetime is due) and **Reporting flags** (all 38 checks run on every report, and what each one catches).

---

## How the month works

The cycle is named for the month the data covers. The **July 2026** cycle is July's numbers, put together and sent in August.

| | |
|---|---|
| Data covered | Jul 1 - Jul 31 |
| Lifetimes included | orders ending through the 3rd business day of August |
| Due | 5th business day of August |

Business days skip federal holidays, not just weekends. The board prints all of this at the top, including how many days you have left.

---

## Step 1 - Watch them arrive

Reports come in by themselves. TapClicks mails them, the tool catches them, checks them immediately, renames them and puts them on the board. You do not have to do anything to start this.

The **Not received** tile has a small dot beside the number:

- **Green, pulsing** - the pull is still running. Hover it for the rate and roughly when the last one lands.
- **Red square** - the pull has stopped. What is left is almost all lifetimes, which somebody has to pull by hand.

**Filenames matter.** Reports join to the board on the order id in the filename first, then on the client name. The convention:

```
July 2026_Client Name 53908.pdf
Lifetime_Client Name 53908.pdf
```

A leading `Lifetime` is what marks a report as a lifetime. The tool renames every file on arrival to its own convention, and if the name it arrived under was wrong you will see a **renamed** tag on the row with the original in the tooltip.

---

## Step 2 - Work your queue

On the board, the **Reports** table defaults to the **Pending** bucket - signed-off rows are hidden, not moved somewhere else. **Completed** and **All** are beside it.

Two things to know about filtering, because there are two rows that look alike:

- The row labelled **Filters** narrows the **partner cards**.
- The row labelled **Reports** narrows the **report rows**.

And: typing in a search box filters the page you are on. **Pressing Enter searches the whole cycle.** That matters, because the table is 50 rows a page and the cards are 20.

### What a row tells you

| Column | What is in it |
|---|---|
| Client | the client, with one pill per order id underneath. Green is live, orange paused, red cancelled, blue complete. Click a pill to open that order in the IO tool. |
| Products | colored product chips, plus a grey pill of the line item ids |
| Kind | **monthly** or **lifetime**. A lifetime shows its flight, and an amber **stopped** tag if the campaign ended early - pull to that date, not the one on the order. |
| Status | see the table below |
| Findings | problems only, up to three then `+4 more` |
| Reporter | who owns it, and who signed it off |

### The statuses

| On screen | Means |
|---|---|
| **Not received** | nothing has arrived and nobody has checked it off |
| **In, unreviewed** | it arrived clean, nobody has looked |
| **Warnings** | worth a look, not a failure |
| **Errors** | at least one failed check nobody has accepted |
| **Needs fix** | you marked it as needing work |
| **Good to go** | signed off, or waived, and clear |

---

## Step 3 - Open a report and judge it

Click any row. The viewer is the checks down the left, the PDF filling the right. The page itself does not scroll - each side scrolls on its own.

### Needs a look

Only real problems are listed. Each one gives you the severity (**fail** or **warn**), where it is (page number and widget name), the title, and the detail. **Investigate** opens the rule's own figures, so if you think a finding is wrong you can see the numbers it used without asking anybody.

For each finding you have one control - the **tick box on the left**:

- **Tick it** to accept. The note stays on the report, it just stops counting against the status. Use this when the flag is real but fine as is, or when it is a known false alarm.
- **Tick it again** to reopen.

**Ticking the last open finding signs the report off automatically** and takes you back to the board. That is intentional and it is the fastest path through a clean-ish report.

### The rest of the panel

- **24 of 27 checks passed** - opens the full list, including what was skipped and why. The brain icon at the top right, on the **Reporting flags** tab, is the same checks written out with what each one catches.
- **Pacing** - spend and impressions, served over ordered, for the month. If it cannot pace it says why instead of showing nothing.
- **Order lines as stored** - what a product finding is actually being judged against. Open this first when you think the tool is wrong about products or dates.
- **Page-one logo** - the exact crop the check compared. If it flags a real partner logo as the default, press **Flag as real logo** and every report carrying that logo gets re-checked.

### Sign it off

| Button | Use it when |
|---|---|
| **Reviewed** | it is right, or the flags on it are accepted |
| **Needs fix** | it needs a new pull or a fix in TapClicks |
| **Waive** | it fails, you are sending it anyway. The failure stays on the record but stops holding up the partner's link. Only appears on a failing report. |

**Save note** is separate from sign-off on purpose. Notes survive re-checks and replacements - use them for what you found and what you told the partner.

You land back on the board where you were, filters and page intact.

---

## Step 4 - Fix and re-run

Three different things, and picking the right one saves you time.

**The report is fine, the checks are old.**
Press **Check this file again** on the viewer. It re-reads the PDF already on disk with today's rules. Accepted findings and the sign-off reset.

**You fixed the PDF yourself.**
Drag it onto **Replace the file**, or press **Upload the corrected PDF**. Same report, same row, your note kept. Every check re-runs. It refuses a file named for a different order rather than quietly filing it in the wrong place.

**It was re-pulled from TapClicks.**
It comes in by itself and replaces the old one - unless the old one was signed off or uploaded by hand. In that case it is parked and the row grows an amber **newer file waiting** tag, and the row goes back to **Pending** even though it is signed off, because there is a decision on it. The sign-off itself is not torn up - the copy that was signed off is still the copy the partner gets until you say otherwise. Open the report and you get **A newer file arrived**, with the incoming file viewable before you choose:

- **Use the new file** - replaces it and re-runs everything. The sign-off resets.
- **Keep this one** - throws the new one away.

### Two things that will surprise you once

**An amber re-check button in the board toolbar is normal.** It sits beside the `orders` button and shows a count of reports still to redo, so it reads as something like `40 checks`. It means those reports were judged by older checking code. A background sweeper is already working through them; pressing the button does them now. It skips anything signed off.

**A report you signed off can come back to you.** If a re-check finds a failure that was not there when you signed it, the sign-off is pulled and the row reads **needs signing off again**. That is the point of it - it will not quietly ship something that now fails.

---

## Step 5 - Rows with no report

On any **Not received** row, in the Sign-off column:

| Control | Use it for |
|---|---|
| **Upload** | you pulled the PDF by hand. Uploading against the row beats whatever the filename says. |
| Teal tick - **Done, no report** | handled this month with no PDF. This is the SEO case. |
| Red slash - **Not needed** | it did not run, nothing is owed |
| **Note** box | why. It stays on the row. |
| Undo arrow | puts the row back the way the rules had it |

All of these are for **this cycle only**. Next month the row is back asking for a report.

Below the table there is a collapsible panel: **N clients not owed a report this cycle**, each with a plain reason - served 3 days, did not serve at all, another order overlaps, lifetime already delivered. If the rule is wrong about one, press **Needs a report** and it goes back on the board.

---

## Step 6 - Package and share

You do not have to wait for a partner to be 100% done.

On the partner card:

- **Package & share** - everything, when the partner is finished.
- **Send the N good to go** - sends only the signed-off ones now. The rest stay out of the folder and join them on the next sync. Nothing is lost.

Packaging runs in the background and shows a live `12 of 30` counter. When it finishes the card carries the **Client link**, a **Copy** button and the file count.

**Packaged Links** (chain icon in the rail) is the page for everything after that:

- Every packaged partner with its link, target and file count.
- **Sync reports** - sends the current reports into the **same folder under the same link**. Only changed files are replaced. This is what you use after a fix goes out, and it is why the card grows an amber **sync reports** button when reports have changed since packaging.
- **Good to go only** - the same, but signed-off reports only.
- **A second link, tagged** - a separate folder with its own link, leaving the original untouched. Use it for a corrected set you do not want to overwrite.
- **Not packaged yet** - partners with something signed off and nothing sent.

If a partner's link went to the wrong place you will see a red **went to Drive, should be Dropbox** badge. The target itself is set on the Partners page.

---

## Two pages worth knowing about

### Lifetimes delivered

**Has this campaign already had its closeout?** A lifetime is owed once, when the campaign ends, and the awkward cases are the ones where the end date moved or the campaign was re-flighted. This page is every lifetime that has gone out, with the client, the partner and the cycle it went in.

Use it when the board is asking for a lifetime you think already went, or when somebody asks whether a finished campaign was ever closed out. If it is on this page, it went. If the board is asking again anyway, the order's end date moved after it was sent - which means it really is owed again, for the new ending.

### Batch history

**Did the report ever arrive?** Every batch the tool has taken in, newest first: the partner, the cycle, how many reports were in it, and how they came out. Open one and you get the reports in that batch.

Use it when a report is sitting on **Not received** and somebody is sure it was sent. If the batch is not there, it never reached the tool - the pull did not run, or it went to the wrong address, and no amount of waiting on the board will fix it. If the batch is there but the report is not on the board, the file arrived under a name that did not join to anything, which is worth flagging.

There is also **Check a batch by hand** at the bottom of that page: drop in PDFs or a zip of them and it runs the same checks as the email path, without touching anybody's cycle.

---

## Step 7 - Check the board against the tracker

**Check a list** (clipboard icon). The board is built from the order export; the reporting tracker is built from what people know. This page says where the two disagree.

Paste the campaign column, or the whole sheet - it finds the column with the order ids in it. Leave Partner blank. Press **Compare** and you get two tables:

- **On the list, not on the board** - with a plain-English reason for each.
- **On the board, not on the list** - with its current status.

Worth doing once a month before you start packaging.

---

## When something looks wrong

**Before anything else, check the build.** It is in the top bar and the footer: `build 2026.08.27-106`. Quote it when you report a problem. A screenshot without it is hard to act on.

| It looks like | It usually is |
|---|---|
| The board is showing the wrong month | the period is pinned in settings. Check the **Cycle** dropdown top right. |
| A finding is clearly wrong about a product or a date | open **Order lines as stored** on the viewer - it shows exactly what the check is comparing against |
| A finding is clearly wrong about a number | open **Investigate** on the finding - it prints the rule's own figures |
| "The stored PDF is gone" | PDFs are pruned after 4 months. Findings and sign-offs are kept. Upload it again if you need it. |
| A partner has no reports at all | its order export may not have landed. Check the **Order list** page. |
| A report never showed up at all | check **Batch history** - if it is not there, it never reached the tool |
| Not sure whether a lifetime already went out | check **Lifetimes delivered** |
| Everything looks stale | press the amber re-check button in the board toolbar (it shows a count, like `40 checks`) |

**Tooltips are the documentation.** Almost every icon button explains itself on hover. If you are not sure what something does, hover it before you press it.

---

## Quick reference

| Where | What it is |
|---|---|
| Name box, top right | attribution for sign-offs. Not a login. Set it once. |
| Brain icon, top right | **Reporting rules** - every rule the board applies, in writing |
| **Filters** row | narrows partner cards |
| **Reports** row | narrows report rows |
| Enter in a search box | searches the whole cycle, not just this page |
| **Views** row | save and reuse a set of filters |
| **Share** button | copies the current board URL, filters and all |
| **Download CSV** | the whole cycle as a spreadsheet |
