PDF Comparison Tool — Flask Edition
=====================================

FILES
-----
  app.py                    Flask server (200 MB upload limit, disk session)
  compare.py                Fast page-by-page diff engine with hash skip
  highlight.py              PDF underline annotations + ReportLab report
  templates/index.html      Upload page (dark UI)
  templates/result.html     Results: metrics, downloads, inline diff, table
  requirements.txt          Python dependencies

INSTALL
-------
  pip install -r requirements.txt

RUN
---
  python app.py
  Open http://localhost:5000

HOW IT WORKS
------------
Pipeline per page:
  1. Hash both page texts — skip identical pages instantly
  2. Whitespace-normalised comparison (ignores spacing differences)
  3. Word-level diff only on changed pages (direct index → rect mapping)
  4. Pages with > 1000 word changes get a summary row instead of flooding output
  5. Only changed pages are annotated and saved

COLOUR CODING (underlines in highlighted PDF)
----------------------------------------------
  GREEN  = Inserted  (new words only in PDF 2)
  YELLOW = Modified  (words that replaced PDF 1 content)
  RED    = Deleted   (shown in report/diff only — words removed from PDF 1)

LIMITS
------
  Max upload   : 200 MB per file
  Word diff cap: 1000 changed words per page before summary mode kicks in
