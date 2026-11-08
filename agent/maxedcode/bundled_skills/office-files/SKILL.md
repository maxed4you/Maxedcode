---
name: office-files
description: Use when a task involves .pdf, .docx, .xlsx/.csv or .pptx files - read, extract text or tables, edit, create, merge, fill forms, convert. read_file cannot show these binary formats.
---
# Office files: PDF, Word, Excel, PowerPoint

Task: $ARGUMENTS

read_file shows these files as binary. Work through py_run (it runs the agent's own Python) with absolute paths.
A library is missing: install it into that same Python and tell the user what was installed:
`import subprocess, sys; r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pypdf"], capture_output=True, text=True); print(r.returncode, r.stderr[-500:])`
(pip takes long: if py_run hits its time limit, run `<sys.executable> -m pip install ...` with powershell.)

General rules:
- Never overwrite the source: write the result next to it (`report_edited.docx`) unless the user asked otherwise.
- Print summaries, not whole documents: page/sheet/slide count and the first part, then only what the task needs.
- After writing, reopen the result and print a short check.

## PDF (pypdf; tables: pdfplumber)
- Text: `PdfReader(p).pages[i].extract_text()`. Empty text on every page = a scan (images): it needs OCR
  (pytesseract + the Tesseract program). Tell the user; do not install Tesseract without asking.
- Tables: `pdfplumber.open(p).pages[i].extract_tables()` (list of rows).
- Merge / split / rotate: `w = PdfWriter(); w.add_page(page); page.rotate(90); w.write(out)`.
- Forms: `PdfReader(p).get_fields()` lists fields. Fill: `w = PdfWriter(clone_from=p)`,
  `w.update_page_form_field_values(w.pages[0], {"name": "value"}, auto_regenerate=False)`,
  `w.set_need_appearances_writer(True)` (otherwise values may be invisible), `w.write(out)`.
- New PDF: reportlab. Cyrillic needs a TTF font: `pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))`,
  otherwise the text comes out as black squares.

## Word .docx (python-docx, pip name `python-docx`)
- Read: `Document(f).paragraphs` (`p.text`, `p.style.name` for headings) and `doc.tables` (`cell.text`).
- Formatting lives in runs. `paragraph.text = ...` drops bold, italic and links. Replace a phrase inside
  `run.text`; if it spans several runs, put the new text into the first run and clear the others.
- Headers/footers: `doc.sections[0].header.paragraphs`. python-docx cannot make tracked changes or
  comments: say so instead of faking them.
- Old `.doc`: convert to .docx with LibreOffice (below) or ask the user to re-save it.

## Excel .xlsx (openpyxl; analysis: pandas)
- `load_workbook(f, data_only=True)` gives the values Excel last saved; `data_only=False` gives formulas.
  Never save a workbook opened with `data_only=True`: all formulas are replaced by values.
- Keep the sheet alive: write formulas, not computed numbers (`ws["C2"] = "=A2*B2"`). openpyxl does not
  calculate; set `wb.calculation.fullCalcOnLoad = True` so Excel recalculates on open, and check the
  expected numbers yourself in Python.
- Keep formatting: edit cells through openpyxl. `pandas.to_excel` rewrites the sheet and loses styles,
  column widths, charts and other sheets.
- `.csv` from Russian Excel: `;` separator and cp1251. Try `encoding="utf-8-sig"`, then `"cp1251"`;
  `sep=None, engine="python"` detects the separator. Write CSV for Excel with `encoding="utf-8-sig"`.
- Check the result for `#REF!`, `#DIV/0!`, `#NAME?`, `#VALUE!` and for formulas pointing at wrong ranges.

## PowerPoint .pptx (python-pptx, pip name `python-pptx`)
- Read: for each `slide` in `Presentation(f).slides`, each `shape` with `shape.has_text_frame`:
  `shape.text_frame.text`; notes: `slide.notes_slide.notes_text_frame.text` if `slide.has_notes_slide`.
- Edit text inside runs, as in Word, to keep formatting. New slide: `prs.slides.add_slide(prs.slide_layouts[1])`
  using the file's own layouts.
- Text does not shrink to fit: keep bullets short, about 6 per slide.

## LibreOffice (optional: .doc/.xls/.ppt, conversion to PDF)
`"C:\Program Files\LibreOffice\program\soffice.exe" --headless --convert-to pdf --outdir <dir> <file>`
(also `--convert-to docx` / `xlsx` / `pptx`). Not installed: say so, do not install it without asking.

Done: report the output path and the check (counts and a short sample of the content).
