"""
compare.py  —  Fast PDF diff engine  (optimised edition)
---------------------------------------------------------

Pipeline per page:
  1. Parallel text extraction using ThreadPoolExecutor
  2. Normalise + hash → skip identical pages instantly
  3. Word-level diff (autojunk=True for 5-10x speedup on large pages)
  4. Pages with > WORD_DIFF_LIMIT changes → one summary row
  5. changes list capped at MAX_CHANGES_ENTRIES to avoid memory bloat
"""

import hashlib
import difflib
import time
import logging
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing

import fitz  # PyMuPDF

log = logging.getLogger(__name__)

# ── Tunable thresholds ────────────────────────────────────────────────────────
WORD_DIFF_LIMIT    = 1000   # pages with more changed words get a summary row
MAX_CHANGES_ENTRIES = 30_000  # cap inline diff entries to keep UI fast
MAX_WORKERS = min(8, (multiprocessing.cpu_count() or 2) * 2)
WHITESPACE_NORM    = True

# ── Colours ───────────────────────────────────────────────────────────────────
COLOR_INSERT = (0.18, 0.66, 0.22)
COLOR_MODIFY = (0.90, 0.72, 0.00)
COLOR_DELETE = (0.86, 0.20, 0.20)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    return " ".join(text.split()) if WHITESPACE_NORM else text


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()


def _extract_page_text(pdf_bytes: bytes, page_idx: int) -> str:
    """Open a fresh doc handle (thread-safe) and extract one page's text."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc[page_idx].get_text()
    finally:
        doc.close()


def _extract_page_words(pdf_bytes: bytes, page_idx: int):
    """Return (words, rects) for one page using a fresh doc handle."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        raw = doc[page_idx].get_text("words", sort=True)
        words = [w[4] for w in raw]
        rects = [fitz.Rect(w[0], w[1], w[2], w[3]) for w in raw]
        return words, rects
    finally:
        doc.close()


# ── Per-page diff (called from thread pool) ───────────────────────────────────

def _diff_page(page_idx, pdf1_bytes, pdf2_bytes, n1, n2):
    """
    Compute diff for one changed page.
    Returns (page_idx, report_rows, changes_fragment, hilites, summary_delta).
    """
    page_label = f"Page {page_idx + 1}"
    report_rows = []
    changes_fragment = []
    hilites = []
    summary_delta = {"inserted": 0, "modified": 0, "deleted": 0}

    words1, _      = _extract_page_words(pdf1_bytes, page_idx) if page_idx < n1 else ([], [])
    words2, rects2 = _extract_page_words(pdf2_bytes, page_idx) if page_idx < n2 else ([], [])

    tok1 = [w.lower() for w in words1]
    tok2 = [w.lower() for w in words2]

    # autojunk=True: use difflib's heuristic — huge speedup on long lists
    # First arg is isjunk (None = no filter); autojunk=True enables the speedup
    matcher = difflib.SequenceMatcher(None, tok1, tok2, autojunk=True)
    opcodes = matcher.get_opcodes()

    page_changes = 0

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(j1, j2):
                changes_fragment.append(("equal", words2[k]))
            continue

        page_changes += (i2 - i1) + (j2 - j1)

        if page_changes > WORD_DIFF_LIMIT:
            report_rows.append({
                "page":        page_label,
                "pdf1_text":   f"[{len(words1)} words]",
                "pdf2_text":   f"[{len(words2)} words]",
                "change_type": "Modified",
            })
            summary_delta["modified"] += 1
            # Use ONE page-spanning rect instead of per-word rects — much faster
            if rects2:
                all_x0 = min(r.x0 for r in rects2)
                all_y0 = min(r.y0 for r in rects2)
                all_x1 = max(r.x1 for r in rects2)
                all_y1 = max(r.y1 for r in rects2)
                import fitz as _fitz
                hilites.append((_fitz.Rect(all_x0, all_y0, all_x1, all_y1), COLOR_MODIFY))
            changes_fragment.append((
                "modify",
                f"[Page {page_idx+1} heavily modified — {len(words1)} → {len(words2)} words]"
            ))
            break

        if tag == "delete":
            text = " ".join(words1[i1:i2])
            changes_fragment.append(("delete", text))
            report_rows.append({
                "page":        page_label,
                "pdf1_text":   text,
                "pdf2_text":   "—",
                "change_type": "Deleted",
            })
            summary_delta["deleted"] += 1

        elif tag == "insert":
            for k in range(j1, j2):
                changes_fragment.append(("insert", words2[k]))
                hilites.append((rects2[k], COLOR_INSERT))
            report_rows.append({
                "page":        page_label,
                "pdf1_text":   "—",
                "pdf2_text":   " ".join(words2[j1:j2]),
                "change_type": "Inserted",
            })
            summary_delta["inserted"] += 1

        elif tag == "replace":
            text_del = " ".join(words1[i1:i2])
            changes_fragment.append(("delete", text_del))
            for k in range(j1, j2):
                changes_fragment.append(("modify", words2[k]))
                hilites.append((rects2[k], COLOR_MODIFY))
            report_rows.append({
                "page":        page_label,
                "pdf1_text":   text_del,
                "pdf2_text":   " ".join(words2[j1:j2]),
                "change_type": "Modified",
            })
            summary_delta["modified"] += 1

    return page_idx, report_rows, changes_fragment, hilites, summary_delta


# ── Main entry point ──────────────────────────────────────────────────────────

def compare_documents(pdf1_bytes, pdf2_bytes, progress_cb=None):
    """
    Compare two PDFs page-by-page using parallel text extraction + diff.

    Returns:
        report_rows, changes, summary, highlights_per_page
    """
    t_total = time.time()

    # Open docs just to get page counts
    with fitz.open(stream=pdf1_bytes, filetype="pdf") as d1:
        n1 = len(d1)
    with fitz.open(stream=pdf2_bytes, filetype="pdf") as d2:
        n2 = len(d2)

    total = max(n1, n2)

    # ── Phase 1: Parallel text extraction for hashing ─────────────────────────
    log.info("Phase 1: extracting text from %d pages (workers=%d)", total, MAX_WORKERS)
    t1 = time.time()

    texts1 = [""] * total
    texts2 = [""] * total

    def fetch_text(pdf_bytes, page_idx, n):
        if page_idx >= n:
            return page_idx, ""
        return page_idx, _extract_page_text(pdf_bytes, page_idx)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs1 = {pool.submit(fetch_text, pdf1_bytes, i, n1): ("1", i) for i in range(total)}
        futs2 = {pool.submit(fetch_text, pdf2_bytes, i, n2): ("2", i) for i in range(total)}
        for f in as_completed({**futs1, **futs2}):
            which, idx = (futs1 if f in futs1 else futs2)[f]
            _, txt = f.result()
            if which == "1":
                texts1[idx] = txt
            else:
                texts2[idx] = txt

    log.info("Phase 1 done in %.2fs", time.time() - t1)

    # ── Phase 2: Identify changed pages via hash ──────────────────────────────
    changed_pages = []
    skipped = 0
    for i in range(min(n1, n2)):
        if _hash(_norm(texts1[i])) != _hash(_norm(texts2[i])):
            changed_pages.append(i)
        else:
            skipped += 1

    log.info("Phase 2: %d changed, %d identical (skipped)", len(changed_pages), skipped)

    # ── Phase 3: Parallel word-level diff on changed pages ────────────────────
    t3 = time.time()
    page_results = {}

    if changed_pages:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futs = {
                pool.submit(_diff_page, idx, pdf1_bytes, pdf2_bytes, n1, n2): idx
                for idx in changed_pages
            }
            done = 0
            for f in as_completed(futs):
                page_idx = futs[f]
                page_results[page_idx] = f.result()
                done += 1
                if progress_cb:
                    progress_cb(done, len(changed_pages))

    log.info("Phase 3 diff done in %.2fs", time.time() - t3)

    # ── Phase 4: Merge results in page order ──────────────────────────────────
    report_rows = []
    changes = []
    highlights_per_page = {}
    summary = {
        "inserted":      0,
        "modified":      0,
        "deleted":       0,
        "skipped_pages": skipped,
        "total_pages":   total,
    }

    for page_idx in sorted(page_results.keys()):
        _, rows, cfrag, hilites, sdelta = page_results[page_idx]
        report_rows.extend(rows)
        # Cap changes list to avoid bloating UI for huge docs
        if len(changes) < MAX_CHANGES_ENTRIES:
            remaining = MAX_CHANGES_ENTRIES - len(changes)
            changes.extend(cfrag[:remaining])
            if len(cfrag) > remaining:
                changes.append(("equal", f"[… {len(cfrag)-remaining} more tokens truncated]"))
        if hilites:
            highlights_per_page[page_idx] = hilites
        for k, v in sdelta.items():
            summary[k] += v

    # ── Extra pages only in one doc ───────────────────────────────────────────
    for page_idx in range(n2, n1):
        words1, _ = _extract_page_words(pdf1_bytes, page_idx)
        text = " ".join(words1)
        report_rows.append({
            "page":        f"Page {page_idx + 1}",
            "pdf1_text":   text[:500] + ("…" if len(text) > 500 else ""),
            "pdf2_text":   "— (page not in PDF 2)",
            "change_type": "Deleted",
        })
        summary["deleted"] += 1

    for page_idx in range(n1, n2):
        words2, rects2 = _extract_page_words(pdf2_bytes, page_idx)
        text = " ".join(words2)
        report_rows.append({
            "page":        f"Page {page_idx + 1}",
            "pdf1_text":   "— (page not in PDF 1)",
            "pdf2_text":   text[:500] + ("…" if len(text) > 500 else ""),
            "change_type": "Inserted",
        })
        summary["inserted"] += 1
        highlights_per_page[page_idx] = [(rects2[k], COLOR_INSERT) for k in range(len(rects2))]

    log.info(
        "TOTAL %.2fs | pages=%d skipped=%d ins=%d mod=%d del=%d workers=%d",
        time.time() - t_total, total, summary["skipped_pages"],
        summary["inserted"], summary["modified"], summary["deleted"], MAX_WORKERS,
    )

    return report_rows, changes, summary, highlights_per_page


# ── Plain-text report ─────────────────────────────────────────────────────────

def build_report_txt(report_rows, pdf1_name, pdf2_name, summary):
    lines = [
        "=" * 80,
        "PDF COMPARISON REPORT",
        "=" * 80,
        f"PDF 1 (original)  : {pdf1_name}",
        f"PDF 2 (modified)  : {pdf2_name}",
        "-" * 80,
        f"Total pages       : {summary.get('total_pages', '?')}",
        f"Identical (skip)  : {summary.get('skipped_pages', 0)}",
        f"Inserted          : {summary['inserted']}",
        f"Modified          : {summary['modified']}",
        f"Deleted           : {summary['deleted']}",
        "=" * 80,
        "",
        f"{'PAGE':<10}{'PDF1 TEXT':<45}{'PDF2 TEXT':<45}{'CHANGE TYPE':<12}",
        "-" * 112,
    ]
    for row in report_rows:
        p1 = str(row["pdf1_text"])[:43]
        p2 = str(row["pdf2_text"])[:43]
        lines.append(f"{row['page']:<10}{p1:<45}{p2:<45}{row['change_type']:<12}")
    lines += ["", "=" * 80]
    return "\n".join(lines).encode("utf-8")
