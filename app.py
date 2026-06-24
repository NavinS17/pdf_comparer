"""
flask_app.py  (rename to app.py when deploying)
-------------------------------------------------
Flask server for the PDF Comparison Tool.

Routes
------
GET  /                  → index.html  (upload page)
POST /compare           → runs comparison, stores results, redirects to /result
GET  /result            → result.html (download + inline diff)
GET  /download/<kind>   → streams file  (highlighted | report_pdf | report_txt)
"""

import os
import uuid
import json
import tempfile
import io
from pathlib import Path
from flask import (
    Flask, request, render_template, redirect, url_for,
    send_file, session, flash,
)

from compare import compare_documents, build_report_txt
from highlight import build_highlighted_pdf, build_report_pdf

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "pdf-compare-secret-2024")

# ── 500 MB upload limit ───────────────────────────────────────────────────────
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024   # 500 MB

# ── Temp directory for all generated / large session data ─────────────────────
TEMP_DIR = Path(tempfile.gettempdir()) / "pdf_compare_sessions"
TEMP_DIR.mkdir(exist_ok=True)


# ── Disk-based session helpers ────────────────────────────────────────────────
# We never store large data (changes list, report rows) in the cookie because
# Flask's signed-cookie session is capped at ~4 KB.  Only the session ID and
# small metadata go into the cookie; everything else lives on disk.

def session_dir(sid: str) -> Path:
    d = TEMP_DIR / sid
    d.mkdir(exist_ok=True)
    return d


def disk_write(sid: str, name: str, data: bytes) -> None:
    (session_dir(sid) / name).write_bytes(data)


def disk_read(sid: str, name: str) -> bytes:
    return (session_dir(sid) / name).read_bytes()


def disk_write_json(sid: str, name: str, obj) -> None:
    disk_write(sid, name, json.dumps(obj).encode())


def disk_read_json(sid: str, name: str):
    return json.loads(disk_read(sid, name).decode())


# ── Diff → HTML ───────────────────────────────────────────────────────────────

def build_diff_html(changes):
    parts = []
    for change_type, text in changes:
        esc = (text.replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;"))
        if change_type == "delete":
            parts.append(f'<span class="diff-del">{esc}</span> ')
        elif change_type == "insert":
            parts.append(f'<span class="diff-ins">{esc}</span> ')
        elif change_type == "modify":
            parts.append(f'<span class="diff-mod">{esc}</span> ')
        else:
            parts.append(esc + " ")
    return "".join(parts)


# ── Error handler for files that exceed the limit ─────────────────────────────
@app.errorhandler(413)
def too_large(e):
    flash("File too large — maximum upload size is 500 MB per file.", "error")
    return redirect(url_for("index"))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/compare", methods=["POST"])
def compare():
    f1 = request.files.get("pdf1")
    f2 = request.files.get("pdf2")

    if not f1 or not f2:
        flash("Please upload both PDF files.", "error")
        return redirect(url_for("index"))

    pdf1_bytes = f1.read()
    pdf2_bytes = f2.read()
    pdf1_name  = f1.filename
    pdf2_name  = f2.filename

    try:
        report_rows, changes, summary, page_diffs = compare_documents(
            pdf1_bytes, pdf2_bytes
        )
        highlighted, annot_done, annot_total = build_highlighted_pdf(pdf2_bytes, page_diffs)
        report_pdf  = build_report_pdf(report_rows, pdf1_name, pdf2_name, summary)
        report_txt  = build_report_txt(report_rows, pdf1_name, pdf2_name, summary)
    except Exception as e:
        flash(f"Comparison failed: {e}", "error")
        return redirect(url_for("index"))

    # Persist everything to disk — never in the cookie
    sid = uuid.uuid4().hex
    disk_write(sid, "highlighted.pdf", highlighted)
    disk_write(sid, "report.pdf",      report_pdf)
    disk_write(sid, "report.txt",      report_txt)
    disk_write_json(sid, "changes.json",      changes)
    disk_write_json(sid, "report_rows.json",  report_rows)
    disk_write_json(sid, "meta.json", {
        "pdf1_name":   pdf1_name,
        "pdf2_name":   pdf2_name,
        "summary":     summary,
        "annot_done":  annot_done,
        "annot_total": annot_total,
    })

    # Only store the tiny session ID in the cookie
    session.clear()
    session["sid"] = sid

    return redirect(url_for("result"))


@app.route("/result")
def result():
    sid = session.get("sid")
    if not sid:
        return redirect(url_for("index"))

    try:
        meta        = disk_read_json(sid, "meta.json")
        changes     = disk_read_json(sid, "changes.json")
        report_rows = disk_read_json(sid, "report_rows.json")
    except FileNotFoundError:
        flash("Session expired. Please compare again.", "error")
        return redirect(url_for("index"))

    diff_html = build_diff_html(changes)

    return render_template(
        "result.html",
        summary     = meta["summary"],
        pdf1_name   = meta["pdf1_name"],
        pdf2_name   = meta["pdf2_name"],
        diff_html   = diff_html,
        report_rows = report_rows,
        annot_done  = meta.get("annot_done", 0),
        annot_total = meta.get("annot_total", 0),
    )


@app.route("/download/<kind>")
def download(kind):
    sid = session.get("sid")
    if not sid:
        return redirect(url_for("index"))

    try:
        meta = disk_read_json(sid, "meta.json")
    except FileNotFoundError:
        return redirect(url_for("index"))

    pdf2_name = meta.get("pdf2_name", "output")

    if kind == "highlighted":
        data     = disk_read(sid, "highlighted.pdf")
        filename = f"highlighted_{pdf2_name}"
        mime     = "application/pdf"
    elif kind == "report_pdf":
        data     = disk_read(sid, "report.pdf")
        filename = "comparison_report.pdf"
        mime     = "application/pdf"
    elif kind == "report_txt":
        data     = disk_read(sid, "report.txt")
        filename = "comparison_report.txt"
        mime     = "text/plain"
    else:
        return "Not found", 404

    return send_file(io.BytesIO(data), mimetype=mime,
                     as_attachment=True, download_name=filename)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
