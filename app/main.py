from __future__ import annotations

from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query,
                     Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import Batch, OrderLine, Report, SessionLocal, init_db
from .ingest import parse_postmark, process_batch
from .orders_s3 import last_sync, sync as sync_orders
from .roster import completeness, import_orders

app = FastAPI(title="Report QA")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _guard(k: str | None):
    if k != settings.inbound_secret:
        raise HTTPException(status_code=403, detail="bad key")


# ---------------------------------------------------------------- inbound email
@app.post("/inbound/mailgun")
async def inbound_mailgun(request: Request, k: str | None = Query(None),
                          db: Session = Depends(get_db)):
    _guard(k)
    form = await request.form()
    sender = str(form.get("sender") or form.get("from") or "")
    subject = str(form.get("subject") or "")
    files: list[tuple[str, bytes]] = []
    for key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            files.append((value.filename, await value.read()))
    if not files:
        return {"ok": True, "skipped": "no attachments"}
    batch = process_batch(db, files, source="mailgun", email_from=sender, subject=subject)
    return {"ok": True, "batch": batch.id, "reports": len(batch.reports)}


@app.post("/inbound/postmark")
async def inbound_postmark(request: Request, k: str | None = Query(None),
                           db: Session = Depends(get_db)):
    _guard(k)
    payload = await request.json()
    sender, subject, files = parse_postmark(payload)
    if not files:
        return {"ok": True, "skipped": "no attachments"}
    batch = process_batch(db, files, source="postmark", email_from=sender, subject=subject)
    return {"ok": True, "batch": batch.id, "reports": len(batch.reports)}


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(Batch).order_by(desc(Batch.received_at)).limit(40)).all()
    latest = batches[0] if batches else None
    comp = completeness(db, latest.market, latest.period) if latest else None
    return templates.TemplateResponse(request, "dashboard.html", {
        "batches": batches, "latest": latest, "comp": comp,
        "orders": db.query(OrderLine).count(),
    })


@app.get("/batch/{batch_id}", response_class=HTMLResponse)
def batch_view(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404)
    comp = completeness(db, batch.market, batch.period)
    order = {"fail": 0, "warn": 1, "pass": 2}
    reports = sorted(batch.reports, key=lambda r: (order.get(r.severity, 3), r.client.lower()))
    return templates.TemplateResponse(request, "batch.html", {
        "batch": batch, "reports": reports, "comp": comp})


@app.get("/report/{report_id}/file")
def report_file(report_id: int, db: Session = Depends(get_db)):
    rep = db.get(Report, report_id)
    if not rep or not rep.stored_path or not Path(rep.stored_path).exists():
        raise HTTPException(404)
    return FileResponse(rep.stored_path, media_type="application/pdf", filename=rep.filename)


# ---------------------------------------------------------------- manual paths
@app.post("/upload")
async def upload(files: list[UploadFile] = File(...), market: str = Form(""),
                 db: Session = Depends(get_db)):
    payload = [(f.filename or "file.pdf", await f.read()) for f in files]
    batch = process_batch(db, payload, source="upload", market=market, subject=market)
    return RedirectResponse(f"/batch/{batch.id}", status_code=303)


@app.get("/orders", response_class=HTMLResponse)
def orders_view(request: Request, db: Session = Depends(get_db)):
    lines = db.scalars(select(OrderLine).order_by(OrderLine.market, OrderLine.client)).all()
    return templates.TemplateResponse(request, "orders.html", {
        "lines": lines, "sync": last_sync(db), "s3": settings.s3_configured,
        "s3_uri": f"s3://{settings.orders_s3_bucket}/{settings.orders_s3_key}"
                  if settings.s3_configured else ""})


@app.post("/orders/import")
async def orders_import(file: UploadFile = File(...), period: str = Form(""),
                        db: Session = Depends(get_db)):
    n = import_orders(db, await file.read(), filename=file.filename or "orders.csv",
                      replace=True, period=period or None)
    return RedirectResponse(f"/orders?imported={n}", status_code=303)


@app.post("/orders/sync")
def orders_sync(db: Session = Depends(get_db)):
    sync_orders(db, force=True)
    return RedirectResponse("/orders", status_code=303)


@app.post("/batch/{batch_id}/renotify")
def renotify(batch_id: int, db: Session = Depends(get_db)):
    from .notify import post_slack, send_digest
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404)
    comp = completeness(db, batch.market, batch.period)
    post_slack(batch, comp)
    send_digest(batch, comp)
    return RedirectResponse(f"/batch/{batch_id}", status_code=303)
