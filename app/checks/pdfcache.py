"""What poppler says about a stored PDF, kept beside it.

"Check this file again" reads a file that has not changed, and most of its
time was poppler doing the same work as last time: pdftotext three ways,
pdfinfo, pdfimages, and a pdftoppm render per page the image checks look at.
The answers are kept in a folder next to the PDF and reused while the file is
the same size with the same timestamp.

ONLY FOR FILES UNDER THE DATA DIRECTORY. A fixture, an upload still in a temp
file - anything else is read fresh and nothing is written beside it.

Every write lands through a temp name and os.replace, so the two workers can
fill the same folder without either reading half a file.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def _root() -> Path | None:
    try:
        from ..config import settings
        return Path(settings.data_dir).resolve()
    except Exception:                                            # noqa: BLE001
        return None


def cache_dir_for(path) -> Path:
    """The folder that holds a PDF's cache, whatever version it is."""
    p = Path(path)
    return p.parent / f".{p.name}.cache"


def _dir(path) -> Path | None:
    try:
        p = Path(path).resolve()
        root = _root()
        if root is None or root not in p.parents:
            return None
        st = p.stat()
    except OSError:
        return None
    base = cache_dir_for(p)
    here = base / f"{st.st_size}-{st.st_mtime_ns}"
    if not here.is_dir():
        # A new version of the file: the old answers go.
        if base.is_dir():
            shutil.rmtree(base, ignore_errors=True)
        try:
            here.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
    return here


def _write(dest: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".w")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def text(path, key: str, make) -> str:
    """make() -> str, kept under `key`."""
    d = _dir(path)
    if d is None:
        return make()
    f = d / f"{key}.txt"
    try:
        return f.read_text(encoding="utf-8")
    except OSError:
        pass
    out = make()
    _write(f, out.encode("utf-8"))
    return out


def page_png(path, page: int, dpi: int) -> bytes | None:
    """The page rendered at dpi, as PNG bytes. None if it would not render."""
    from .. import proc as _proc
    from .parser import _bin

    d = _dir(path)
    f = d / f"p{page}-{dpi}.png" if d is not None else None
    if f is not None:
        try:
            return f.read_bytes()
        except OSError:
            pass
    with tempfile.TemporaryDirectory() as tmp:
        _proc.run([_bin("pdftoppm"), "-r", str(dpi), "-f", str(page),
                   "-l", str(page), "-png", str(path), f"{tmp}/p"],
                  capture_output=True, timeout=120)
        hits = sorted(Path(tmp).glob("p*.png"))
        if not hits:
            return None
        data = hits[0].read_bytes()
    if f is not None:
        _write(f, data)
    return data


def page_image(path, page: int, dpi: int):
    """The page as a PIL image, or None."""
    import io

    from PIL import Image
    data = page_png(path, page, dpi)
    if data is None:
        return None
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def sweep(root) -> int:
    """Remove caches whose PDF is gone. Returns how many."""
    n = 0
    for c in Path(root).glob("batch-*/.*.cache"):
        pdf = c.parent / c.name[1:-len(".cache")]
        if not pdf.exists():
            shutil.rmtree(c, ignore_errors=True)
            n += 1
    return n
