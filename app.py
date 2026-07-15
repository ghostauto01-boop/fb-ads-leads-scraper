"""
app.py
------
Flask backend for the FB Ads Leads Scraper (DTC brand discovery agent).

Routes
------
  GET  /                         -> UI (templates/index.html)
  POST /api/discover             -> start a discovery job  {url, target_brands, pages}
  GET  /api/job/<id>             -> job status / logs / final table
  GET  /api/download/<id>/<file> -> brands.csv | brands.xlsx
  GET  /health                   -> liveness probe for Render
"""

from __future__ import annotations

import csv
import io
import os
import threading
import time
import uuid
from typing import Dict, List

from flask import Flask, jsonify, render_template, request, send_file, abort
from flask_cors import CORS

import scraper as scraper_mod
import discovery as discovery_mod

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)

# ---------------------------------------------------------------------------
# In-memory job store (single gunicorn worker -> safe enough for the free tier)
# ---------------------------------------------------------------------------
_JOBS: Dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

OUTPUT_COLUMNS = [
    "brand_name", "website", "emails", "country", "category",
    "active_ads_count", "activity_level", "instagram_handle",
    "instagram_followers", "platform", "dtc_score", "smb_score", "total_score",
]


def _fmt_followers(n) -> str:
    if n is None:
        return "Unknown"
    try:
        n = int(n)
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}k"
        return str(n)
    except Exception:
        return "Unknown"


def _brand_for_ui(b: dict) -> dict:
    return {
        "brand_name": b.get("brand_name", ""),
        "website": b.get("website", ""),
        "website_url": b.get("website_url") or ("https://" + b.get("website", "")),
        "emails": ", ".join(b.get("emails", []) or []),
        "country": b.get("country", ""),
        "category": b.get("category", "Other"),
        "active_ads_count": b.get("active_ads_count", 0),
        "activity_level": b.get("activity_level", "Low"),
        "instagram_handle": b.get("instagram_handle", ""),
        "instagram_followers": b.get("instagram_followers"),
        "instagram_followers_display": _fmt_followers(b.get("instagram_followers")),
        "platform": b.get("platform", "unknown"),
        "dtc_score": b.get("dtc_score", 0),
        "smb_score": b.get("smb_score", 0),
        "total_score": b.get("total_score", 0),
    }


# ---------------------------------------------------------------------------
# Background pipeline
# ---------------------------------------------------------------------------
def _discovery_pipeline(job_id: str, url: str, target: int, pages: int) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job["status"] = "running"
        job["started_at"] = time.time()

    def log(msg: str):
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is not None:
                j["logs"].append(msg)
                j["updated_at"] = time.time()

    try:
        log(f"[job] Starting discovery: target={target} pages={pages}")
        log(f"[job] URL: {url}")
        raw_ads = scraper_mod.scrape_ads_library(url, pages=pages, log=log)
        log(f"[job] Scraped {len(raw_ads)} raw ad entries.")

        brands = discovery_mod.discover_brands(
            raw_ads, target=target, original_url=url,
            log=log, pages=pages, scraper_fn=scraper_mod.scrape_ads_library,
        )

        ui_rows = [_brand_for_ui(b) for b in brands]
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is not None:
                j["brands"] = ui_rows
                j["status"] = "completed"
                j["finished_at"] = time.time()
        log(f"[job] DONE — {len(ui_rows)} brands ready.")
    except Exception as e:
        log(f"[job] ERROR: {e}")
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j is not None:
                j["status"] = "failed"
                j["error"] = str(e)
                j["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    # Lightweight check that the process + deps import OK.
    return jsonify({
        "status": "ok",
        "time": int(time.time()),
        "jobs": len(_JOBS),
    })


@app.route("/api/discover", methods=["POST"])
def api_discover():
    data = request.get_json(silent=True) or request.form or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url'."}), 400

    try:
        target = int(data.get("target_brands", 50))
    except (TypeError, ValueError):
        target = 50
    try:
        pages = int(data.get("pages", 40))
    except (TypeError, ValueError):
        pages = 40

    target = max(1, min(target, 100))
    pages = max(1, min(pages, 80))

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "url": url,
            "target": target,
            "pages": pages,
            "logs": [],
            "brands": [],
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
            "started_at": None,
            "finished_at": None,
        }

    thread = threading.Thread(
        target=_discovery_pipeline, args=(job_id, url, target, pages), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"})


@app.route("/api/job/<job_id>")
def api_job(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        snapshot = {
            "id": job["id"],
            "status": job["status"],
            "url": job["url"],
            "target": job["target"],
            "pages": job["pages"],
            "logs": list(job["logs"]),
            "brands": list(job["brands"]),
            "error": job["error"],
            "brand_count": len(job["brands"]),
        }
    return jsonify(snapshot)


@app.route("/api/download/<job_id>/<file>")
def api_download(job_id, file):
    file = (file or "").lower()
    if file not in ("brands.csv", "brands.xlsx"):
        abort(404)
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            abort(404)
        brands = list(job["brands"])
    if not brands:
        abort(404)

    if file == "brands.csv":
        path = _write_csv(job_id, brands)
        return send_file(path, as_attachment=True, download_name="brands.csv",
                         mimetype="text/csv")
    else:
        path = _write_xlsx(job_id, brands)
        return send_file(path, as_attachment=True, download_name="brands.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------
def _write_csv(job_id: str, brands: List[dict]) -> str:
    path = os.path.join(OUTPUT_DIR, f"{job_id}_brands.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(OUTPUT_COLUMNS)
        for b in brands:
            writer.writerow([
                b.get("brand_name", ""),
                b.get("website", ""),
                b.get("emails", ""),
                b.get("country", ""),
                b.get("category", ""),
                b.get("active_ads_count", ""),
                b.get("activity_level", ""),
                b.get("instagram_handle", ""),
                ("" if b.get("instagram_followers") is None else b.get("instagram_followers")),
                b.get("platform", ""),
                b.get("dtc_score", ""),
                b.get("smb_score", ""),
                b.get("total_score", ""),
            ])
    return path


def _write_xlsx(job_id: str, brands: List[dict]) -> str:
    try:
        from openpyxl import Workbook
    except Exception:
        # fall back to CSV if openpyxl is missing
        return _write_csv(job_id, brands)
    path = os.path.join(OUTPUT_DIR, f"{job_id}_brands.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Brands"
    ws.append(OUTPUT_COLUMNS)
    for b in brands:
        ws.append([
            b.get("brand_name", ""),
            b.get("website", ""),
            b.get("emails", ""),
            b.get("country", ""),
            b.get("category", ""),
            b.get("active_ads_count", ""),
            b.get("activity_level", ""),
            b.get("instagram_handle", ""),
            ("" if b.get("instagram_followers") is None else b.get("instagram_followers")),
            b.get("platform", ""),
            b.get("dtc_score", ""),
            b.get("smb_score", ""),
            b.get("total_score", ""),
        ])
    # widen columns a touch
    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 12), 40)
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Periodic cleanup of old jobs (best-effort)
# ---------------------------------------------------------------------------
def _cleanup_old_jobs(max_age_seconds: int = 6 * 3600):
    while True:
        time.sleep(600)
        now = time.time()
        with _JOBS_LOCK:
            stale = [jid for jid, j in _JOBS.items()
                     if now - j.get("updated_at", now) > max_age_seconds]
            for jid in stale:
                _JOBS.pop(jid, None)


threading.Thread(target=_cleanup_old_jobs, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
