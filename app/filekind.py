"""What kind of file a report is, when it is not a PDF.

Everything here was written for PDFs, because everything that arrives through
the feed is one. SEO is not: it is pulled outside TapClicks by hand and some of
it comes back as a PowerPoint deck. Those reports still have to be stored,
named, signed off and packaged into the partner's folder - they just cannot be
read by the checks, which is already true of every SEO report and is why the
skip exists.

So this is the one place that answers "what is this file", and the rest of the
tool asks rather than assuming.
"""
from __future__ import annotations

from pathlib import Path

PDF = "pdf"
PPTX = "pptx"

# A pptx is a zip. So is a docx, an xlsx and a jar - the magic bytes alone do
# not tell them apart, which is why the extension has to agree.
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

MEDIA = {
    PDF: "application/pdf",
    PPTX: ("application/vnd.openxmlformats-officedocument"
           ".presentationml.presentation"),
}


def kind_of_blob(blob: bytes, filename: str = "") -> str:
    """"pdf", "pptx", or "" for anything else."""
    if blob[:5] == b"%PDF-":
        return PDF
    if (blob[:4] in ZIP_MAGIC
            and (filename or "").lower().strip().endswith(".pptx")):
        return PPTX
    return ""


def kind_of_path(path) -> str:
    """The kind of a file already on disk, by its name."""
    return PPTX if str(path or "").lower().endswith(".pptx") else PDF


def extension(kind: str) -> str:
    return ".pptx" if kind == PPTX else ".pdf"


def media_type(kind: str) -> str:
    return MEDIA.get(kind, MEDIA[PDF])


def is_pdf(path) -> bool:
    return kind_of_path(path) == PDF


def slide_count(path) -> int:
    """How many slides are in a deck, for the "19 pages" line on the board.

    Read out of the package rather than by rendering it - there is no
    PowerPoint on this box and there is not going to be. Returns 0 if the file
    will not open, which is what the page count already does for a PDF that
    will not.
    """
    import zipfile

    try:
        with zipfile.ZipFile(Path(path)) as z:
            return sum(1 for n in z.namelist()
                       if n.startswith("ppt/slides/slide")
                       and n.endswith(".xml"))
    except Exception:                                        # noqa: BLE001
        return 0
