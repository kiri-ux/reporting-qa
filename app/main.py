from __future__ import annotations

from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from . import brand, version
from .config import settings
from .db import Batch, OrderLine, OrderSync, Report, SessionLocal, init_db
from .ingest import finish_batch, parse_postmark, process_batch
from .orders_s3 import last_sync, sync as sync_orders
from .roster import completeness, import_orders

app = FastAPI(title="Report QA")

_HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

templates = Jinja2Templates(directory=str(_HERE / "templates"))
# Chrome that every page needs and no view should have to remember to pass.
templates.env.globals.update(
    head_tags=brand.HEAD_TAGS,
    build_label=version.label(),
    build_notes=version.BUILD_NOTES,
    build_service=version.service(),
    nav="",
)


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
    """Includes the build so you can confirm what is actually live without
    trusting the dashboard, which looks identical either way."""
    return {"ok": True, **version.info()}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=brand.FAVICON_SVG, media_type="image/svg+xml")


def _finish(batch_id: int) -> None:
    """Runs after the webhook has already answered."""
    db = SessionLocal()
    try:
        finish_batch(db, batch_id)
    finally:
        db.close()


def _guard(k: str | None):
    if k != settings.inbound_secret:
        raise HTTPException(status_code=403, detail="bad key")


# ---------------------------------------------------------------- inbound email
@app.post("/inbound/mailgun")
async def inbound_mailgun(request: Request, background: BackgroundTasks,
                          k: str | None = Query(None), db: Session = Depends(get_db)):
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
    batch = process_batch(db, files, source="mailgun", email_from=sender,
                          subject=subject, notify=False)
    background.add_task(_finish, batch.id)
    return {"ok": True, "batch": batch.id, "reports": len(batch.reports)}


@app.post("/inbound/postmark")
async def inbound_postmark(request: Request, background: BackgroundTasks,
                           k: str | None = Query(None), db: Session = Depends(get_db)):
    _guard(k)
    payload = await request.json()
    sender, subject, files = parse_postmark(payload)
    if not files:
        return {"ok": True, "skipped": "no attachments"}
    batch = process_batch(db, files, source="postmark", email_from=sender,
                          subject=subject, notify=False)
    background.add_task(_finish, batch.id)
    return {"ok": True, "batch": batch.id, "reports": len(batch.reports)}


# ---------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    batches = db.scalars(select(Batch).order_by(desc(Batch.received_at)).limit(40)).all()
    latest = batches[0] if batches else None
    comp = completeness(db, latest.market, latest.period) if latest else None
    return templates.TemplateResponse(request, "dashboard.html", {
        "batches": batches, "latest": latest, "comp": comp, "nav": "dash",
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
        "batch": batch, "reports": reports, "comp": comp, "nav": "dash"})


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
        "nav": "orders",
        "env_report": settings.env_report(),
        "s3_uri": f"s3://{settings.orders_s3_bucket}/{settings.orders_s3_key}"
                  if settings.s3_configured else ""})


@app.post("/orders/import")
async def orders_import(file: UploadFile = File(...), period: str = Form(""),
                        db: Session = Depends(get_db)):
    res = import_orders(db, await file.read(), filename=file.filename or "orders.csv",
                        replace=True, period=period or None)
    n = res["kept"] if isinstance(res, dict) else res
    if isinstance(res, dict):
        msg = (f"Imported {n} order lines from an upload, "
               f"{res.get('rows_read', 0):,} rows read")
        if res.get("duplicate_rows"):
            msg += f", {res['duplicate_rows']:,} duplicate rows ignored"
        db.add(OrderSync(source=f"upload: {file.filename}", rows=n, ok=True,
                         message=msg + ".", guidance=res.get("guidance") or {}))
        db.commit()
    return RedirectResponse(f"/orders?imported={n}", status_code=303)


@app.post("/orders/sync")
def orders_sync(db: Session = Depends(get_db)):
    """A failed sync must land back on /orders with the reason on screen. A
    bare 500 tells you nothing and hides the message the sync already wrote."""
    try:
        sync_orders(db, force=True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        db.rollback()
        try:
            db.add(OrderSync(source="", ok=False, rows=0,
                             message=f"Sync crashed: {type(exc).__name__}: {exc}"))
            db.commit()
        except Exception:  # noqa: BLE001 - never let logging the error be the error
            db.rollback()
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
