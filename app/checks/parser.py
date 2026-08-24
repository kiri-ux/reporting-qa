"""Turn a TapClicks digital marketing report PDF into structured tables.

Everything here was derived by hand from real 7 Mountains reports. The two
things that trip up a naive parser are form-feed characters prefixing the first
line of each page, and wrapped line-item names that look like section headings.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

METRIC_LABELS = [
    "Impressions", "Clicks", "CTR", "X the National Avg (.07%)",
    "Video Completion Rate", "Events", "Event Rate", "View-throughs",
    "Click Conversions", "View-through Conversions", "DOOH Ads Served",
]
CORE = ("Impressions", "Clicks", "CTR")


def _bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise RuntimeError(f"{name} not found. Install poppler-utils.")
    return p


def pdf_text(path: Path, first: int | None = None, last: int | None = None) -> str:
    cmd = [_bin("pdftotext"), "-layout"]
    if first:
        cmd += ["-f", str(first)]
    if last:
        cmd += ["-l", str(last)]
    cmd += [str(path), "-"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return out.stdout.replace("\x0c", "")


def page_count(path: Path) -> int:
    out = subprocess.run([_bin("pdfinfo"), str(path)], capture_output=True, text=True, timeout=60)
    m = re.search(r"Pages:\s+(\d+)", out.stdout)
    return int(m.group(1)) if m else 0


def page_ink_pct(path: Path, page: int, dpi: int = 50) -> float:
    """Share of dark pixels below the header band. Near zero means the page has
    text but no chart or table, which is how an empty widget shows up."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as d:
        subprocess.run(
            [_bin("pdftoppm"), "-r", str(dpi), "-f", str(page), "-l", str(page), "-png",
             str(path), f"{d}/p"],
            capture_output=True, timeout=120,
        )
        files = list(Path(d).glob("p*.png"))
        if not files:
            return 100.0
        im = Image.open(files[0]).convert("L")
        w, h = im.size
        im = im.crop((0, int(h * 0.10), w, h))
        px = im.load()
        total = dark = 0
        for y in range(0, im.size[1], 3):
            for x in range(0, im.size[0], 3):
                total += 1
                if px[x, y] < 235:
                    dark += 1
        return dark / total * 100 if total else 100.0


def tokens(line: str) -> list[tuple[str, int, int]]:
    return [
        (m.group().strip(), m.start(), m.end())
        for m in re.finditer(r"\S+(?: \S+)*?(?=\s{2,}|\s*$)", line)
        if m.group().strip()
    ]


def as_number(tok: str) -> float | None:
    t = tok.replace(",", "").replace("%", "").replace("$", "").strip()
    return float(t) if re.fullmatch(r"-?\d+(\.\d+)?", t) else None


@dataclass
class Table:
    title: str
    rows: list[tuple[str, dict[str, float]]] = field(default_factory=list)

    def total(self, metric: str) -> float:
        return sum(v.get(metric, 0.0) for n, v in self.rows if n.strip().lower() != "total")

    @property
    def body(self):
        return [(n, v) for n, v in self.rows if n.strip().lower() != "total"]


SKIP_LINE = re.compile(r"(Digital Marketing Report|Date range|Created On|Powered by TCPDF)")


def extract_tables(text: str, strict: bool = True) -> list[Table]:
    """A table starts at a header line carrying at least three metric labels.
    strict=True keeps only rows that resolve Impressions, Clicks and CTR, which
    is what stops conversion breakouts leaking into line-item totals."""
    lines = text.split("\n")
    out: list[Table] = []
    need = 3 if strict else 2

    for i, line in enumerate(lines):
        cols = tokens(line)
        labelled = [c for c in cols if c[0] in METRIC_LABELS]
        if len(labelled) < need:
            continue
        if any(as_number(c[0]) is not None for c in cols):
            continue

        title = ""
        for j in range(i - 1, max(-1, i - 10), -1):
            s = lines[j].strip()
            if not s or len(s) < 4 or SKIP_LINE.search(lines[j]):
                continue
            if len([c for c in tokens(lines[j]) if c[0] in METRIC_LABELS]) >= need:
                break
            title = s
            break

        table = Table(title=title)
        name_col_end = labelled[0][1] if labelled else 0
        for j in range(i + 1, len(lines)):
            raw = lines[j]
            if not raw.strip() or SKIP_LINE.search(raw) or raw.lstrip().startswith("*Note"):
                continue
            cells = tokens(raw)
            if len([c for c in cells if c[0] in METRIC_LABELS]) >= need:
                break
            values: dict[str, float] = {}
            for tok, _start, end in cells:
                n = as_number(tok)
                if n is None:
                    continue
                best, best_d = None, 99
                for label, _ls, le in labelled:
                    d = abs(end - le)
                    if d < best_d:
                        best_d, best = d, label
                if best_d <= 4 and best:
                    values.setdefault(best, n)

            # A wrapped row name continues on the next line with no numbers in
            # the metric columns. Append it, or the product suffix that decides
            # device eligibility ("... B2B CTV", "... Mobile") is lost.
            if not values and table.rows and cells:
                head = cells[0]
                looks_like_heading = bool(
                    re.search(r"(Conversions|Performance|Breakout|by Day|by Strategy|"
                              r"by Ad Size|Screenshots|Publishers)", raw))
                if (head[1] < name_col_end and as_number(head[0]) is None
                        and not looks_like_heading and len(cells) == 1):
                    prev_name, prev_vals = table.rows[-1]
                    table.rows[-1] = ((prev_name + " " + raw.strip()).strip(), prev_vals)
                continue
            if not values:
                continue
            if strict and not all(k in values for k in CORE):
                continue
            name = cells[0][0] if cells and as_number(cells[0][0]) is None else ""
            table.rows.append((name, values))
        if table.rows:
            out.append(table)
    return out


HEADLINE_FULL = re.compile(
    r"How many ads were served:.*?\n\n\s*([\d,]+)\s+([\d,]+)\s+([\d.]+)%", re.S)
HEADLINE_IMPS = re.compile(r"How many ads were served:\s*\n\s*([\d,]+)")


def headline(text: str) -> tuple[float | None, float | None, float | None]:
    m = HEADLINE_FULL.search(text)
    if m:
        return as_number(m.group(1)), as_number(m.group(2)), as_number(m.group(3))
    m = HEADLINE_IMPS.search(text)
    if m:
        return as_number(m.group(1)), None, None
    return None, None, None


CLIENT_RE = re.compile(r"Digital Marketing Report for (.+?)\s*$", re.M)
PERIOD_RE = re.compile(r"Date range (\w{3} \d{2}, \d{4}) to (\w{3} \d{2}, \d{4})")


def meta_from_text(text: str) -> dict:
    client = ""
    m = CLIENT_RE.search(text)
    if m:
        client = m.group(1).strip()
    period = ""
    p = PERIOD_RE.search(text)
    if p:
        from dateutil import parser as dp
        period = dp.parse(p.group(1)).strftime("%Y-%m")
    return {"client": client, "period": period}


FILENAME_RE = re.compile(r"^(?P<prefix>[A-Za-z]+ \d{4}|Lifetime)_(?P<rest>.+)$")
ACCOUNTS_RE = re.compile(r"\b\d{4,6}\b")


def meta_from_filename(name: str) -> dict:
    stem = Path(name).stem
    is_lifetime = stem.lower().startswith("lifetime")
    m = FILENAME_RE.match(stem)
    rest = m.group("rest") if m else stem
    accounts = ACCOUNTS_RE.findall(rest)
    client = rest
    for a in accounts:
        client = client.replace(a, " ")
    client = re.sub(r"[\s:_/-]+$", "", re.sub(r"\s{2,}", " ", client)).strip(" _-:/")
    return {"client": client, "account_ids": " ".join(accounts), "is_lifetime": is_lifetime}
