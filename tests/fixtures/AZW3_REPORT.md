# AZW3 extraction verification — `mobi` 0.4.1

Date: 2026-11-09. Sample: `sample-book.azw3` ("A Small Guide to Paper Boats", Ada Example).
Verdict: **extraction works.** `mobi` 0.4.1 (kindleunpack backend) extracts a complete,
readable EPUB (HTML + CSS + images) from the real DRM-free AZW3 sample.

## 1. Sample file

- Path: `~/workspace/manyue-reader/tests/fixtures/sample-book.azw3`
- Source: https://99helpers.com/file-types/azw3
  (direct link https://99helpers.com/samples/azw3/sample-book.azw3, HTTP 200)
- Size: **60,454 bytes** (matches the page's stated 60,454 bytes / 59.0 KB)
- Magic: bytes 60–68 = `BOOKMOBI`; PalmDB name field `A_Small_Guide_to_Paper_Boats`
- MOBI header (record 0, offset 232): header length 264, **file version 8** (KF8/AZW3),
  MOBI type 2, text encoding 65001 (UTF-8), **encryption type 0 = no DRM**
- 19 records: text in rec 1–2 (PalmDOC-compressed, 3 flows), cover PNG rec 12
  (26,599 B), JPEG thumbnail rec 13 (10,233 B), folding-diagram PNG rec 14 (9,152 B)

## 2. Extraction call (what the app must actually do)

API note — the task brief guessed `mobi.extract(infile, outdir)`, but the real
0.4.1 API (see `/tmp/piptest/mobi/extract.py`) is:

```python
import mobi
tempdir, outpath = mobi.extract("/path/to/book.azw3")   # ONE argument, no outdir
```

It creates its own `tempfile.mkdtemp(prefix="mobiex")` dir, runs
`kindleunpack.unpackBook(infile, tempdir, epubver="A")`, and returns
`(tempdir, path)`. For this KF8 file it returned:

```
('/tmp/mobiex5il2bs4o', '/tmp/mobiex5il2bs4o/mobi8/sample-book.epub')
```

in ~0.01 s. There is **no single "main HTML" output**: the primary result is a
**rebuilt EPUB** (`mobi8/`), not a flat HTML file. (The `mobi7/` fallback
`book.html` is only produced for MOBI-6 files; this KF8-only file has no MOBI 6
part, so no `book.html` exists — the app must unzip the EPUB and read its XHTML.)

Run with `PYTHONPATH=/tmp/piptest` (package + deps `loguru`, `standard-imghdr`
live there). No calibre involved.

## 3. Output directory tree (exact layout)

```
/tmp/mobiex5il2bs4o/
├── mobi8/
│   ├── sample-book.epub            37,389 B   ← the returned path
│   ├── mimetype                    20 B
│   ├── META-INF/container.xml      251 B
│   └── OEBPS/
│       ├── content.opf             2,895 B
│       ├── toc.ncx                998 B
│       ├── Styles/style0001.css   57 B
│       ├── Styles/style0002.css   1,665 B
│       ├── Text/cover_page.xhtml  606 B
│       ├── Text/part0000.xhtml    738 B   (Contents)
│       ├── Text/part0001.xhtml    1,197 B (Ch 1. Choosing the paper)
│       ├── Text/part0002.xhtml    2,077 B (Ch 2. Folding the classic boat)
│       ├── Text/part0003.xhtml    1,065 B (Ch 3. Launching and floating)
│       ├── Images/cover00012.png  26,599 B (600×900 PNG, the cover)
│       └── Images/image00014.png  9,152 B  (600×260 PNG, folding diagram)
└── mobi7/
    └── Images/
        ├── cover00012.png         26,599 B
        ├── image00013.jpeg         10,233 B (160×240 JPEG thumbnail — mobi7 only)
        └── image00014.png         9,152 B
```

Notes:
- The EPUB is a normal zip; unzipping it gives exactly the `mobi8/` tree minus
  `sample-book.epub` itself.
- The JPEG thumbnail (`image00013.jpeg`, EXTH 202) is extracted to `mobi7/Images/`
  only — it is **not** inside the EPUB. The EPUB contains only the cover PNG and
  the diagram PNG.
- `mobi7/` contains **no HTML file** for this book (no `book.html`).

## 4. HTML structure and image references

- **Split into parts, not one file**: 5 XHTML files — `cover_page.xhtml` +
  `part0000.xhtml` (Contents) + `part0001/2/3.xhtml` (one per chapter).
  There is no single "main HTML"; the reading order is `cover_page` →
  `part0000` → `part0001` → `part0002` → `part0003` (spine order in
  `content.opf`, NCX in `toc.ncx`).
- **Encoding**: every file starts `<?xml version="1.0" encoding="UTF-8"?>`,
  `<html xmlns="http://www.w3.org/1999/xhtml" lang="en">`, UTF-8 throughout.
- **Image references are relative, from `Text/` to `../Images/`**:
  - `cover_page.xhtml`: `<image … xlink:href="../Images/cover00012.png"/>`
    (inside an `<svg viewBox="0 0 600 900">`)
  - `part0002.xhtml`: `<img src="../Images/image00014.png" alt="Four folding
    steps: fold in half, fold the corners down, fold the brims up, open the
    hull." class="calibre6"/>`
  - No other XHTML file references an image.
- **CSS references are relative too**: every part links
  `../Styles/style0002.css` and `../Styles/style0001.css`.
- **Cover**: yes — `OEBPS/Images/cover00012.png`, 600×900 PNG, 26,599 B,
  referenced from `cover_page.xhtml`; `content.opf` has
  `<meta name="cover" content="cover_img" />` and metadata
  title "A Small Guide to Paper Boats", creator "Ada Example", language "en".
- First 30 lines of `part0000.xhtml` (Contents page):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
  <head>
    <title>Contents</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
  <link rel="stylesheet" type="text/css" href="../Styles/style0002.css"/>
<link rel="stylesheet" type="text/css" href="../Styles/style0001.css"/>
</head>
  <body class="calibre">
<h1 class="calibre1">Contents</h1>
<ol class="calibre2">
<li class="calibre3"><a href="part0001.xhtml#ch1">Chapter 1. Choosing the paper</a></li>
<li class="calibre3"><a href="part0002.xhtml#ch2">Chapter 2. Folding the classic boat</a></li>
<li class="calibre3"><a href="part0003.xhtml#ch3">Chapter 3. Launching and floating</a></li>
</ol>
</body>
</html>
```

  Chapter pages use the same skeleton (`<body class="calibre">`,
  `<h1 id="chN" class="calibre1">…</h1>`, `<p class="calibre4">…</p>`).
  Ch 2 also contains a numbered list and a bordered table with caption
  ("Paper and how long it floated in a bath test").

## 5. Text completeness

- Visible-text character counts (tags stripped, whitespace collapsed):
  `cover_page.xhtml` 114, `part0000` 117, `part0001` 659, `part0002` 771,
  `part0003` 504 — **total 2,165 characters**.
- Text is complete and readable: all three chapters, contents page, figure
  caption, list and table content present; no mojibake, no missing chapters.
  Sample (Ch 1): "Almost any rectangle of paper will make a boat, but some
  float far longer than others. A sheet of ordinary printer paper is the usual
  starting point: it folds cleanly, holds a crease and survives a few minutes
  on a pond."
- Minor quirk: each chapter's `<h1>` title text appears twice in the
  tag-stripped count (kindleunpack duplicates the heading in the body), which
  accounts for the small overlap in the counts above — the rendered pages
  themselves are correct.

## 6. Corruption tests (failure mode)

`mobi.extract()` on a corrupted copy — results by corruption site:

| Corruption | Result |
|---|---|
| 10 bytes flipped at offsets 30000–30009 (inside image records) | **No error** — extraction succeeds, images may be damaged |
| 10 bytes flipped at offsets 9400–9409 (inside text record 1) | **No error** — EPUB produced, but text is garbled (`?xml verrsion=…`, `Choosodi the paper`) and visible text drops 2,165 → 1,254 chars |
| File truncated to first 30,000 bytes (images cut off) | **No error** — EPUB produced with full text (2,173 chars) but no diagram image and no stylesheets |
| `BOOKMOBI` magic overwritten with `XXXXXXXX` (bytes 60–68) | **Raises `unpackException: Invalid file format`** |
| Record-0 header bytes flipped (offsets 232–241, incl. compression field) | **Raises `unpackException: invalid compression type: 0xfffd`** |

Implication for the app: **do not rely on `mobi.extract()` raising for all
corrupt files.** Header/magic corruption raises a clean `unpackException`
(good for a "cannot parse" message), but mid-file corruption and truncation
silently produce degraded output. The app should additionally validate
beforehand (check `BOOKMOBI` at bytes 60–68 and MOBI version 8) and sanity-check
the result afterwards (e.g. EPUB exists, XHTML parts non-empty, text length
plausible). Note the `unpackException` type comes from kindleunpack
(`mobi.kindleunpack`); catch `Exception` broadly to be safe.

## 7. What the reader app needs to do (summary, no code written)

1. Call `mobi.extract(path)` → `(tempdir, epub_path)`; on `unpackException`
   (or any exception) show "cannot parse".
2. Unzip the returned EPUB; read `OEBPS/content.opf` for spine order and
   metadata; render `OEBPS/Text/*.xhtml` in spine order.
3. Resolve relative `../Images/…` and `../Styles/…` references inside the
   unzipped EPUB tree (they are plain relative paths — no `kindle:` URLs
   survive in the output).
4. Cover = `OEBPS/Images/cover00012.png` (or the file named by the OPF
   `cover` meta); the JPEG thumbnail is only in `mobi7/Images/` and is not
   needed for reading.
5. `tempdir` is a fresh `mkdtemp` dir per call — the app owns cleanup.
