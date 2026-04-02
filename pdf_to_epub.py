#!/usr/bin/env python3
"""
pdf_to_epub.py – Convert a scanned PDF to a searchable text EPUB using OCR.

Requirements
------------
    pip install pymupdf pytesseract Pillow

Tesseract OCR must also be installed on your system:
    macOS:   brew install tesseract
    Ubuntu:  sudo apt install tesseract-ocr
    Windows: https://github.com/UB-Mannheim/tesseract/wiki

Additional Tesseract language packs (if you need non-English OCR):
    Ubuntu:  sudo apt install tesseract-ocr-<lang>   e.g. tesseract-ocr-fra
    macOS:   brew install tesseract-lang

Usage examples
--------------
    python pdf_to_epub.py scan.pdf
    python pdf_to_epub.py scan.pdf --title "My Book" --lang fra
    python pdf_to_epub.py scan.pdf --output my_book.epub --lang deu --scale 3.0

Supported OCR language codes (--lang)
--------------------------------------
    eng  English (default)   fra  French       deu  German
    spa  Spanish             ita  Italian       por  Portuguese
    rus  Russian             chi_sim  Chinese (Simplified)
    jpn  Japanese            ara  Arabic
"""

import argparse
import datetime
import re
import sys
import zipfile
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit(
        "PyMuPDF is not installed.\n"
        "Run:  pip install pymupdf"
    )

try:
    import pytesseract
    from PIL import Image
except ImportError:
    sys.exit(
        "pytesseract or Pillow is not installed.\n"
        "Run:  pip install pytesseract Pillow"
    )

# Tesseract language code → BCP-47 language tag (used in EPUB metadata / XHTML)
_LANG_MAP = {
    'eng': 'en',
    'fra': 'fr',
    'deu': 'de',
    'spa': 'es',
    'ita': 'it',
    'por': 'pt',
    'rus': 'ru',
    'chi_sim': 'zh-Hans',
    'jpn': 'ja',
    'ara': 'ar',
}


# ── XML / filename helpers ────────────────────────────────────────────────────

def _escape_xml(text: str) -> str:
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&apos;')
    )


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[^a-z0-9_\-. ]', '_', name, flags=re.IGNORECASE).strip() or 'book'


# ── EPUB content builders ─────────────────────────────────────────────────────

def _make_xhtml(title: str, page_num: int, text: str, lang: str) -> str:
    escaped = _escape_xml(text)
    lines = escaped.split('\n')
    body = '\n'.join(
        f'<p>{line}</p>' if line.strip() else '<p>&#160;</p>'
        for line in lines
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"'
        f' xml:lang="{_escape_xml(lang)}" lang="{_escape_xml(lang)}">\n'
        '<head>'
        '<meta charset="utf-8"/>'
        f'<title>{_escape_xml(title)} \u2014 Page {page_num}</title>'
        '</head>\n'
        '<body>\n'
        f'<h2>Page {page_num}</h2>\n'
        f'{body or "<p> </p>"}\n'
        '</body>\n'
        '</html>'
    )


def _make_opf(uid: str, title: str, lang: str, spine_items: list) -> str:
    manifest_items = '\n    '.join(
        f'<item id="page{i + 1}" href="{f}" media-type="application/xhtml+xml"/>'
        for i, f in enumerate(spine_items)
    )
    spine_refs = '\n    '.join(
        f'<itemref idref="page{i + 1}"/>'
        for i in range(len(spine_items))
    )
    modified = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="uid">{_escape_xml(uid)}</dc:identifier>\n'
        f'    <dc:title>{_escape_xml(title)}</dc:title>\n'
        f'    <dc:language>{_escape_xml(lang)}</dc:language>\n'
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n'
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
        f'    {manifest_items}\n'
        '  </manifest>\n'
        '  <spine toc="ncx">\n'
        f'    {spine_refs}\n'
        '  </spine>\n'
        '</package>'
    )


def _make_ncx(uid: str, title: str, spine_items: list) -> str:
    nav_points = '\n  '.join(
        f'<navPoint id="np{i + 1}" playOrder="{i + 1}">'
        f'<navLabel><text>Page {i + 1}</text></navLabel>'
        f'<content src="{f}"/>'
        f'</navPoint>'
        for i, f in enumerate(spine_items)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
        '  <head>'
        f'<meta name="dtb:uid" content="{_escape_xml(uid)}"/>'
        '</head>\n'
        f'  <docTitle><text>{_escape_xml(title)}</text></docTitle>\n'
        '  <navMap>\n'
        f'  {nav_points}\n'
        '  </navMap>\n'
        '</ncx>'
    )


def _make_nav(title: str, spine_items: list, lang: str) -> str:
    items = '\n      '.join(
        f'<li><a href="{f}">Page {i + 1}</a></li>'
        for i, f in enumerate(spine_items)
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"'
        f' xmlns:epub="http://www.idpf.org/2007/ops"'
        f' xml:lang="{_escape_xml(lang)}" lang="{_escape_xml(lang)}">\n'
        '<head><meta charset="utf-8"/><title>Table of Contents</title></head>\n'
        '<body>\n'
        '  <nav epub:type="toc">\n'
        '    <h1>Table of Contents</h1>\n'
        '    <ol>\n'
        f'      {items}\n'
        '    </ol>\n'
        '  </nav>\n'
        '</body>\n'
        '</html>'
    )


# ── Core: build the EPUB zip ──────────────────────────────────────────────────

def build_epub(output_path: Path, title: str, page_texts: list, bcp47: str) -> None:
    """Write a valid EPUB 3 archive to *output_path*."""
    uid = f'book-{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}'
    spine_items = [f'page{i + 1}.xhtml' for i in range(len(page_texts))]

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # mimetype must be the first entry and stored uncompressed
        info = zipfile.ZipInfo('mimetype')
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, 'application/epub+zip')

        zf.writestr(
            'META-INF/container.xml',
            '<?xml version="1.0"?>'
            '<container version="1.0"'
            ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles>'
            '<rootfile full-path="OEBPS/content.opf"'
            ' media-type="application/oebps-package+xml"/>'
            '</rootfiles>'
            '</container>',
        )

        for i, text in enumerate(page_texts):
            zf.writestr(f'OEBPS/page{i + 1}.xhtml', _make_xhtml(title, i + 1, text, bcp47))

        zf.writestr('OEBPS/content.opf', _make_opf(uid, title, bcp47, spine_items))
        zf.writestr('OEBPS/toc.ncx', _make_ncx(uid, title, spine_items))
        zf.writestr('OEBPS/nav.xhtml', _make_nav(title, spine_items, bcp47))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog='pdf_to_epub.py',
        description='Convert a scanned PDF to a searchable text EPUB using OCR.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'examples:\n'
            '  python pdf_to_epub.py scan.pdf\n'
            '  python pdf_to_epub.py scan.pdf --title "My Book" --lang fra\n'
            '  python pdf_to_epub.py scan.pdf --output book.epub --lang deu --scale 3.0\n'
            '\n'
            'supported --lang values:\n'
            '  eng  English (default)   fra  French       deu  German\n'
            '  spa  Spanish             ita  Italian       por  Portuguese\n'
            '  rus  Russian             chi_sim  Chinese (Simplified)\n'
            '  jpn  Japanese            ara  Arabic\n'
        ),
    )
    parser.add_argument('pdf', help='path to the scanned PDF file')
    parser.add_argument(
        '--output', '-o',
        help='output EPUB path (default: same directory and stem as the PDF)',
    )
    parser.add_argument(
        '--title', '-t',
        help='book title written into EPUB metadata (default: PDF filename stem)',
    )
    parser.add_argument(
        '--lang', '-l',
        default='eng',
        metavar='CODE',
        help='Tesseract OCR language code (default: eng)',
    )
    parser.add_argument(
        '--scale',
        type=float,
        default=2.0,
        metavar='N',
        help='render scale for better OCR quality (default: 2.0; higher = slower but more accurate)',
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        sys.exit(f'Error: file not found: {pdf_path}')
    if pdf_path.suffix.lower() != '.pdf':
        print(f'Warning: {pdf_path.name} does not have a .pdf extension', file=sys.stderr)

    title = args.title or pdf_path.stem or 'My Book'
    bcp47 = _LANG_MAP.get(args.lang, args.lang)
    output_path = Path(args.output) if args.output else pdf_path.with_suffix('.epub')

    print(f'Input:    {pdf_path}')
    print(f'Output:   {output_path}')
    print(f'Title:    {title}')
    print(f'Language: {args.lang} ({bcp47})')
    print(f'Scale:    {args.scale}x')

    # ── Load PDF ──────────────────────────────────────────────────────────────
    print('Loading PDF…')
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    print(f'Pages:    {total}')

    # ── OCR each page ─────────────────────────────────────────────────────────
    matrix = fitz.Matrix(args.scale, args.scale)
    page_texts = []
    for i in range(total):
        print(f'  OCR page {i + 1}/{total}…', end='\r', flush=True)
        page = doc[i]
        pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
        img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
        text = pytesseract.image_to_string(img, lang=args.lang)
        page_texts.append(text.strip())
    doc.close()
    print(f'  OCR complete — {total} page(s) processed.          ')

    # ── Build EPUB ────────────────────────────────────────────────────────────
    print('Building EPUB…')
    build_epub(output_path, title, page_texts, bcp47)
    size_kb = output_path.stat().st_size / 1024
    print(f'Done! Saved to: {output_path} ({size_kb:.1f} KB)')


if __name__ == '__main__':
    main()
