#!/usr/bin/env python3
"""
pdf_to_epub.py – Convert a scanned PDF to a searchable text EPUB using OCR.

What it does
------------
  • OCR each page with Tesseract (via pytesseract)
  • Embeds a JPEG thumbnail for pages with fewer than 50 OCR words (likely
    figures or tables) so visual content is not lost — use --no-images to skip
  • Links a CSS stylesheet for readable typography

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
    python pdf_to_epub.py scan.pdf --no-images

Supported OCR language codes (--lang)
--------------------------------------
    eng  English (default)   fra  French       deu  German
    spa  Spanish             ita  Italian       por  Portuguese
    rus  Russian             chi_sim  Chinese (Simplified)
    jpn  Japanese            ara  Arabic
"""

import argparse
import datetime
import io
import re
import sys
import zipfile
from collections import defaultdict
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


# ── Stylesheet ────────────────────────────────────────────────────────────────

STYLESHEET = """\
body {
  font-family: Georgia, 'Times New Roman', Times, serif;
  font-size: 1em;
  line-height: 1.6;
  max-width: 38em;
  margin: 0 auto;
  padding: 1em 1.5em 3em;
  color: #1a1a1a;
}
p {
  margin-top: 0;
  margin-bottom: 0.8em;
  text-align: justify;
}
h1, h2, h3, h4 {
  font-family: Georgia, serif;
  font-weight: bold;
  line-height: 1.3;
  margin-top: 1.8em;
  margin-bottom: 0.4em;
}
h2 { font-size: 1.2em; }
h3 { font-size: 1.05em; }
figure {
  margin: 1.5em 0;
  text-align: center;
}
figure img {
  max-width: 100%;
  height: auto;
}
figcaption {
  font-size: 0.9em;
  color: #555;
  margin-top: 0.3em;
}
"""

# Pages with fewer than this many OCR words are treated as figure/table pages
# and have their rendered image embedded.
_FIGURE_WORD_THRESHOLD = 50

# Maximum pixel width for embedded page images (keeps file size reasonable)
_FIGURE_MAX_WIDTH = 800


def _get_jpeg_bytes(img: 'Image.Image', max_width: int = _FIGURE_MAX_WIDTH) -> bytes:
    """Downscale *img* to at most *max_width* pixels wide and return JPEG bytes."""
    w, h = img.size
    if w > max_width:
        new_h = int(h * max_width / w)
        img = img.resize((max_width, new_h), resample=1)  # LANCZOS=1 in Pillow ≥9
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='JPEG', quality=75, optimize=True)
    return buf.getvalue()


# Height ratios (relative to page median word height) that indicate a heading.
# Words printed in larger type → bounding box height is proportionally taller.
# Thresholds are intentionally conservative: noisy scanned-document bounding
# boxes mean that body text lines sometimes measure 1.2-1.3× due to ascending
# characters, OCR merge, or ink spread.  A heading needs to be noticeably
# larger to be reliably distinguished.
_H2_HEIGHT_RATIO = 1.55   # ≥155 % of median → main section heading (OCR bboxes)
_H3_HEIGHT_RATIO = 1.30   # ≥130 % of median → sub-section heading (OCR bboxes)

# Font-size ratios for native text heading detection (digital PDFs with a text layer).
# Lower than the OCR equivalents since precise vector font metrics are available.
_H2_NATIVE_RATIO = 1.15   # ≥115 % of body median → main section heading
_H3_NATIVE_RATIO = 1.05   # ≥105 % of body median → sub-section heading

# Minimum fraction of alphabetic characters that must be UPPERCASE before a
# height-promoted line is accepted as a heading.  This gate prevents sentence-
# case body text (e.g. "they would be injected by …") from being promoted.
_HEADING_UPPER_FRAC = 0.70


_IMPERATIVE_STARTERS = frozenset({
    'ATTACH', 'BEND', 'BUILD', 'CHECK', 'CONNECT', 'COVER', 'CUT', 'DO',
    'DRILL', 'FILL', 'FIT', 'FOLD', 'GLUE', 'HOLD', 'INSERT', 'KEEP',
    'MAKE', 'MARK', 'MOUNT', 'NOTE', 'PLACE', 'PULL', 'PUSH', 'PUT',
    'REMOVE', 'SCREW', 'SECURE', 'SEE', 'SET', 'SLIDE', 'TAPE', 'TIE',
    'TRIM', 'TURN', 'TWIST', 'TO', 'USE', 'WRAP', 'WARNING',
})


def _has_mixed_case_word(text: str) -> bool:
    """Return True if any word has non-standard mixed casing (OCR garbage indicator).

    Normal casing patterns: ALL-UPPER, all-lower, Title-case (First char upper, rest lower).
    Anything else (e.g. 'SdO1LS', 'LOAId') indicates upside-down / mirrored OCR garbage.
    """
    for word in text.split():
        alpha = [c for c in word if c.isalpha()]
        if len(alpha) < 3:
            continue
        is_all_upper = all(c.isupper() for c in alpha)
        is_all_lower = all(c.islower() for c in alpha)
        is_title = alpha[0].isupper() and all(c.islower() for c in alpha[1:])
        if not (is_all_upper or is_all_lower or is_title):
            return True
    return False


def _looks_like_heading(text: str) -> bool:
    """Return True if *text* plausibly looks like a section heading.

    Used as a second gate alongside the height-ratio check so that normal body
    text whose bounding boxes happen to measure tall doesn't become a heading.
    """
    t = text.strip()
    alpha = [c for c in t if c.isalpha()]
    if len(alpha) < 4:
        return False
    # Sentence-case / OCR fragment: bad first character
    first = t[0] if t else ''
    if first in ('(', '[', '{', "'", '"', ',', '.', '\u2018', '\u2019'):
        return False
    # Structural patterns (always accept before other filters so numbered
    # headings like "1. INTRODUCTION" or "2.1. BACKGROUND" are not rejected
    # by the digit-start guard below).
    if re.match(r'^(?:Chapter|Appendix)\s+', t, re.IGNORECASE):
        return True
    if re.match(r'^Part\s+[IVX\d]', t, re.IGNORECASE):
        return True
    if re.match(r'^Section\s+\d', t, re.IGNORECASE):   # "Section 3" not "SECTION A-A"
        return True
    # Numbered section headings: "1. TITLE", "3.1 TITLE", "2.1. TITLE"
    # Requires ALL-CAPS start after the number so "6. Mobile radiation…" is rejected.
    if re.match(r'^\d+(?:\.\d+)*\.?\s+[A-Z]{2}', t):
        return True
    # Digit-start: page labels, OCR garbage (e.g. "98ed 'SNOLLONULSNI 7'")
    if first.isdigit():
        return False
    # Lowercase first alpha char: sentence-case body text (e.g. "he REMOWABLE")
    if first.isalpha() and first.islower():
        return False
    # Too many non-alpha non-space chars (OCR garbage, catalog numbers, equations)
    non_alpha_ns = sum(1 for c in t if not c.isalpha() and not c.isspace())
    total_ns = sum(1 for c in t if not c.isspace())
    if total_ns > 0 and non_alpha_ns / total_ns > 0.30:
        return False
    # Require mostly uppercase
    upper_frac = sum(1 for c in alpha if c.isupper()) / len(alpha)
    if upper_frac < _HEADING_UPPER_FRAC:
        return False
    # Reject if any word has non-standard mixed casing (OCR garbage: "SdO1LS", "LOAId")
    if _has_mixed_case_word(t):
        return False
    # Reject imperative-verb starters (diagram instructions, callouts)
    words = t.split()
    first_word = re.sub(r'[^A-Z]', '', words[0].upper()) if words else ''
    if first_word in _IMPERATIVE_STARTERS:
        return False
    return True


# Caption patterns for inline figure/table extraction
# \d matches Arabic numerals; [IVX\d] also covers Roman numeral figure labels
# like "FIG I", "FIG II", "FIG. IV" which appear in some government publications.
_FIG_CAPTION_RE  = re.compile(r'^FIG(?:URE)?\.?\s*[IVX\d]', re.IGNORECASE)
_TABLE_HEADER_RE = re.compile(r'^TABLE\s+[IVX\d]',           re.IGNORECASE)

# Minimum page-height fraction a cropped region must occupy to be a figure
_MIN_FIGURE_HEIGHT_FRAC = 0.08


def _extract_embedded_images(
    page: 'fitz.Page',
    pil_img: 'Image.Image',
    scale: float,
) -> list[tuple[int, int, bytes]]:
    """Extract embedded raster images from a *digital* PDF page.

    Returns a list of ``(top_px, bottom_px, jpeg_bytes)`` for each embedded
    image that covers at least 5 % of the page area, sorted top-to-bottom.
    For scanned PDFs (where the entire page is one raster layer) this returns
    an empty list because there are no separate image objects.
    """
    try:
        img_infos = page.get_image_info(xrefs=True)
    except Exception:
        return []

    page_area = page.rect.width * page.rect.height
    img_w, img_h = pil_img.size
    results: list[tuple[int, int, bytes]] = []

    for info in img_infos:
        bbox = info.get('bbox')
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox
        # Skip images that are too small to be a real figure
        if page_area > 0 and ((x1 - x0) * (y1 - y0)) / page_area < 0.05:
            continue
        # Skip images that cover almost the entire page — those are full-page
        # background scans embedded in searchable/OCR-layered PDFs, not figures.
        if page_area > 0 and ((x1 - x0) * (y1 - y0)) / page_area > 0.85:
            continue
        # Convert PDF-point coordinates → pixel coordinates
        px0 = max(0, int(x0 * scale))
        py0 = max(0, int(y0 * scale))
        px1 = min(img_w, int(x1 * scale))
        py1 = min(img_h, int(y1 * scale))
        if px1 <= px0 or py1 <= py0:
            continue
        crop = pil_img.crop((px0, py0, px1, py1))
        results.append((py0, py1, _get_jpeg_bytes(crop)))

    return sorted(results, key=lambda t: t[0])


def _ocr_page_with_layout(
    img: 'Image.Image', lang: str,
) -> tuple[list[tuple[str, str]], int, list[tuple[bytes, str]]]:
    """OCR *img* and return ``(content, word_count, inline_figs)``.

    *content* is an ordered list of ``(tag, text)`` tuples where *tag* is
    ``'h2'``, ``'h3'``, or ``'p'``.  Paragraph boundaries (blank lines) are
    represented as ``('p', '\\xa0')``.  When a figure caption (``FIG. N.`` /
    ``TABLE N.``) is detected, a ``('figure-img', str_index)`` entry is
    injected at the correct position and the corresponding cropped-JPEG is
    appended to *inline_figs* as ``(jpeg_bytes, caption_text)``.

    *word_count* counts OCR words excluding figure-region words (used for the
    whole-page figure threshold).

    Heading levels are inferred from the median bounding-box *height* of words
    on each line relative to the page-wide median.  Lines set in noticeably
    larger type are promoted to ``<h2>`` or ``<h3>`` without any text-pattern
    matching — the visual layout drives the decision.

    Falls back gracefully: if ``image_to_data`` is unavailable or returns no
    usable data the function returns an empty list and ``word_count = 0``.
    """
    try:
        data = pytesseract.image_to_data(
            img, lang=lang, output_type=pytesseract.Output.DICT
        )
    except Exception:
        return [], 0, []

    n = len(data.get('text', []))
    words: list[dict] = []
    for i in range(n):
        text = str(data['text'][i]).strip()
        try:
            conf = int(data['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if text and conf > 0:
            words.append({
                'block_num': int(data['block_num'][i]),
                'par_num':   int(data['par_num'][i]),
                'line_num':  int(data['line_num'][i]),
                'text':      text,
                'height':    int(data['height'][i]),
                'top':       int(data['top'][i]),
                'left':      int(data['left'][i]),
            })

    if not words:
        return [], 0, []

    # Page-level median word height (body-text baseline)
    heights = sorted(w['height'] for w in words)
    page_median = heights[len(heights) // 2]

    # Group words into lines by (block_num, par_num, line_num)
    lines_map: dict = defaultdict(list)
    for w in words:
        lines_map[(w['block_num'], w['par_num'], w['line_num'])].append(w)

    # Build ordered line list with vertical positions
    lines_with_pos: list[dict] = []
    for key in sorted(lines_map, key=lambda k: min(w['top'] for w in lines_map[k])):
        line_words = sorted(lines_map[key], key=lambda w: w['left'])
        line_text = ' '.join(w['text'] for w in line_words).strip()
        if not line_text:
            continue
        top_y    = min(w['top']                  for w in line_words)
        bottom_y = max(w['top'] + w['height']    for w in line_words)
        # Infer heading tag
        if page_median > 0:
            line_heights = sorted(w['height'] for w in line_words)
            lm = line_heights[len(line_heights) // 2]
            ratio = lm / page_median
            if ratio >= _H2_HEIGHT_RATIO and _looks_like_heading(line_text):
                tag = 'h2'
            elif ratio >= _H3_HEIGHT_RATIO and _looks_like_heading(line_text):
                tag = 'h3'
            else:
                tag = 'p'
        else:
            tag = 'p'
        lines_with_pos.append({
            'key': key, 'tag': tag, 'text': line_text,
            'top': top_y, 'bottom': bottom_y, 'block': key[0],
        })

    # ── Caption-based figure detection ───────────────────────────────────────
    # FIG. N. captions appear BELOW the figure they describe.
    # TABLE N. headers appear ABOVE the table they introduce.
    img_h, img_w = img.height, img.width
    inline_figs: list[tuple[bytes, str]] = []
    # figure_ranges: set of (top_y, bottom_y) pixel ranges that belong to a figure
    figure_y_ranges: list[tuple[int, int]] = []
    caption_line_set: set[int] = set()  # indices of caption/header lines to skip

    for li, line in enumerate(lines_with_pos):
        text = line['text']

        if _FIG_CAPTION_RE.match(text):
            # Figure region = page top (or end of previous body text) → top of caption.
            # Only lines with 5+ words count as body-text anchors; shorter lines
            # (box labels, callouts inside the figure) are treated as part of the figure.
            fig_bottom = line['top']
            fig_top = 0
            for prev_li in range(li - 1, -1, -1):
                prev = lines_with_pos[prev_li]
                if prev_li in caption_line_set:
                    continue
                if len(prev['text'].split()) >= 5:
                    # Use the bottom of that body-text line as the figure start
                    fig_top = prev['bottom'] + 4
                    break
            region_h = fig_bottom - fig_top
            if region_h >= img_h * _MIN_FIGURE_HEIGHT_FRAC:
                # Include a small margin below the caption so text wraps into figcaption
                cap_bottom = line['bottom'] + 4
                crop = img.crop((0, max(0, fig_top), img_w, min(img_h, cap_bottom)))
                idx = len(inline_figs)
                inline_figs.append((_get_jpeg_bytes(crop), text))
                figure_y_ranges.append((fig_top, cap_bottom))
                caption_line_set.add(li)

        elif _TABLE_HEADER_RE.match(text):
            # Table region = header line top → next section heading / large gap
            tbl_top = line['top']
            tbl_bottom = img_h  # default: end of page
            for next_li in range(li + 1, len(lines_with_pos)):
                nxt = lines_with_pos[next_li]
                # Stop at next h2/h3 or at a large vertical gap (> 3× line height)
                line_h = max(1, line['bottom'] - line['top'])
                if nxt['tag'] in ('h2', 'h3'):
                    tbl_bottom = nxt['top'] - 4
                    break
                if (nxt['top'] - lines_with_pos[next_li - 1]['bottom']) > 3 * line_h:
                    tbl_bottom = nxt['top'] - 4
                    break
            region_h = tbl_bottom - tbl_top
            if region_h >= img_h * _MIN_FIGURE_HEIGHT_FRAC:
                crop = img.crop((0, max(0, tbl_top), img_w, min(img_h, tbl_bottom)))
                idx = len(inline_figs)
                inline_figs.append((_get_jpeg_bytes(crop), text))
                figure_y_ranges.append((tbl_top, tbl_bottom))
                caption_line_set.add(li)

    # ── Build final content list ──────────────────────────────────────────────
    def _in_figure_region(top_y: int, bottom_y: int) -> bool:
        """True if this line overlaps a detected figure region."""
        for fy0, fy1 in figure_y_ranges:
            if top_y < fy1 and bottom_y > fy0:
                return True
        return False

    content: list[tuple[str, str]] = []
    word_count = len(words)
    prev_block: int | None = None
    # Track which figure-img entries have been injected
    inserted_fig_idxs: set[int] = set()

    for li, line in enumerate(lines_with_pos):
        top_y = line['top']
        bottom_y = line['bottom']

        # Before this line, inject any figures whose region ends before this line
        for fi, (fy0, fy1) in enumerate(figure_y_ranges):
            if fi not in inserted_fig_idxs and fy1 <= top_y:
                content.append(('figure-img', str(fi)))
                inserted_fig_idxs.add(fi)

        # Skip lines inside figure regions (captions are included in the JPEG)
        if li in caption_line_set or _in_figure_region(top_y, bottom_y):
            continue

        # Paragraph separator on block change
        block_num = line['block']
        if prev_block is not None and block_num != prev_block:
            if content and content[-1] != ('p', '\xa0'):
                content.append(('p', '\xa0'))
        prev_block = block_num

        content.append((line['tag'], line['text']))

    # Append any remaining un-injected figures at the end
    for fi in range(len(figure_y_ranges)):
        if fi not in inserted_fig_idxs:
            content.append(('figure-img', str(fi)))

    return content, word_count, inline_figs


def _has_native_text(page: 'fitz.Page', min_chars: int = 100) -> bool:
    """Return True if *page* has a meaningful native text layer.

    PDFs where the entire page content is rasterised (pure scans) return False;
    PDFs with an embedded text layer — whether from native typography or from
    an OCR overlay — return True.
    """
    try:
        return len(page.get_text("text").strip()) >= min_chars
    except Exception:
        return False


def _extract_native_page_content(
    page: 'fitz.Page',
    pil_img: 'Image.Image',
    scale: float,
) -> tuple[list[tuple[str, str]], int, list[tuple[bytes, str]]]:
    """Extract content from a *digital* PDF page using its native text layer.

    Replaces OCR for pages that already have an embedded text layer (digital
    PDFs or searchable PDFs with an OCR overlay).  Returns
    ``(content, word_count, inline_figs)`` with the same structure as
    :func:`_ocr_page_with_layout` so the rest of the pipeline is unchanged.

    Heading levels are inferred from font-size ratios relative to the page-wide
    median font size, using :data:`_H2_NATIVE_RATIO` and
    :data:`_H3_NATIVE_RATIO`.  Figure captions are detected with the same
    regex patterns used for OCR, but figure bounding boxes are derived from
    native text coordinates rather than pixel measurements.
    """
    try:
        page_dict = page.get_text("dict")
    except Exception:
        return [], 0, []

    blocks = [b for b in page_dict.get("blocks", []) if b.get("type") == 0]
    if not blocks:
        return [], 0, []

    img_h, img_w = pil_img.height, pil_img.width

    # ── Build line list sorted top-to-bottom ─────────────────────────────────
    lines_with_pos: list[dict] = []
    all_font_sizes: list[float] = []

    for bi, block in enumerate(sorted(blocks, key=lambda b: b["bbox"][1])):
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            # Normalise multi-space runs (PDF text layers often have spacing artefacts)
            raw_text = " ".join(s.get("text", "") for s in spans)
            line_text = re.sub(r'  +', ' ', raw_text).strip()
            if not line_text:
                continue
            font_sizes = [s.get("size", 0.0) for s in spans if s.get("text", "").strip()]
            if not font_sizes:
                continue
            line_font = max(font_sizes)
            all_font_sizes.extend(font_sizes)
            bbox = line.get("bbox", (0.0, 0.0, 0.0, 0.0))
            lines_with_pos.append({
                'text':     line_text,
                'font':     line_font,
                'top':      bbox[1],   # PDF points
                'bottom':   bbox[3],
                'block_id': bi,
                'tag':      'p',       # filled in below
            })

    if not lines_with_pos:
        return [], 0, []

    # Page-wide median font size (body text baseline)
    sorted_sizes = sorted(all_font_sizes)
    page_median = sorted_sizes[len(sorted_sizes) // 2] if sorted_sizes else 10.0

    # Assign heading tags based on font-size ratio + text-pattern gate
    for line in lines_with_pos:
        ratio = (line['font'] / page_median) if page_median > 0 else 1.0
        if ratio >= _H2_NATIVE_RATIO and _looks_like_heading(line['text']):
            line['tag'] = 'h2'
        elif ratio >= _H3_NATIVE_RATIO and _looks_like_heading(line['text']):
            line['tag'] = 'h3'

    # ── Caption-based figure detection ───────────────────────────────────────
    # Same logic as _ocr_page_with_layout(); coordinates are in PDF points and
    # are converted to pixels only when cropping the PIL image.
    inline_figs: list[tuple[bytes, str]] = []
    figure_y_ranges: list[tuple[int, int]] = []   # pixel coordinates
    caption_line_set: set[int] = set()

    for li, line in enumerate(lines_with_pos):
        text = line['text']

        if _FIG_CAPTION_RE.match(text):
            fig_bottom_pt = line['top']
            fig_top_pt = 0.0
            for prev_li in range(li - 1, -1, -1):
                prev = lines_with_pos[prev_li]
                if prev_li in caption_line_set:
                    continue
                # Only 5+-word lines count as body-text anchors
                if len(prev['text'].split()) >= 5:
                    fig_top_pt = prev['bottom'] + 2
                    break
            fig_top_px    = max(0,     int(fig_top_pt        * scale))
            fig_bottom_px = min(img_h, int(fig_bottom_pt     * scale))
            cap_bottom_px = min(img_h, int(line['bottom']    * scale))
            region_h = fig_bottom_px - fig_top_px
            if region_h >= img_h * _MIN_FIGURE_HEIGHT_FRAC:
                crop = pil_img.crop((0, fig_top_px, img_w, cap_bottom_px))
                inline_figs.append((_get_jpeg_bytes(crop), text))
                figure_y_ranges.append((fig_top_px, cap_bottom_px))
                caption_line_set.add(li)

        elif _TABLE_HEADER_RE.match(text):
            tbl_top_pt    = line['top']
            line_h_pt     = max(1.0, line['bottom'] - line['top'])
            tbl_bottom_pt = lines_with_pos[-1]['bottom']
            for next_li in range(li + 1, len(lines_with_pos)):
                nxt = lines_with_pos[next_li]
                if nxt['tag'] in ('h2', 'h3'):
                    tbl_bottom_pt = nxt['top'] - 2
                    break
                if (nxt['top'] - lines_with_pos[next_li - 1]['bottom']) > 3 * line_h_pt:
                    tbl_bottom_pt = nxt['top'] - 2
                    break
            tbl_top_px    = max(0,     int(tbl_top_pt    * scale))
            tbl_bottom_px = min(img_h, int(tbl_bottom_pt * scale))
            region_h = tbl_bottom_px - tbl_top_px
            if region_h >= img_h * _MIN_FIGURE_HEIGHT_FRAC:
                crop = pil_img.crop((0, tbl_top_px, img_w, tbl_bottom_px))
                inline_figs.append((_get_jpeg_bytes(crop), text))
                figure_y_ranges.append((tbl_top_px, tbl_bottom_px))
                caption_line_set.add(li)

    # ── Build final content list ──────────────────────────────────────────────
    def _pt_to_px(y: float) -> int:
        return int(y * scale)

    def _in_figure(top_px: int, bottom_px: int) -> bool:
        return any(top_px < fy1 and bottom_px > fy0 for fy0, fy1 in figure_y_ranges)

    content: list[tuple[str, str]] = []
    word_count = sum(len(l['text'].split()) for l in lines_with_pos)
    inserted_fig_idxs: set[int] = set()
    prev_block_id: int | None = None

    for li, line in enumerate(lines_with_pos):
        top_px    = _pt_to_px(line['top'])
        bottom_px = _pt_to_px(line['bottom'])

        # Inject any figures whose region ends before this line
        for fi, (fy0, fy1) in enumerate(figure_y_ranges):
            if fi not in inserted_fig_idxs and fy1 <= top_px:
                content.append(('figure-img', str(fi)))
                inserted_fig_idxs.add(fi)

        # Skip lines inside figure regions (caption text is in the JPEG)
        if li in caption_line_set or _in_figure(top_px, bottom_px):
            continue

        # Paragraph separator on block change
        bid = line['block_id']
        if prev_block_id is not None and bid != prev_block_id:
            if content and content[-1] != ('p', '\xa0'):
                content.append(('p', '\xa0'))
        prev_block_id = bid

        content.append((line['tag'], line['text']))

    # Append any remaining un-injected figures
    for fi in range(len(figure_y_ranges)):
        if fi not in inserted_fig_idxs:
            content.append(('figure-img', str(fi)))

    return content, word_count, inline_figs

def _make_xhtml(title: str, page_num: int,
                content: 'list[tuple[str, str]] | str', lang: str,
                image_ref: str | None = None,
                inline_fig_refs: 'list[tuple[str, str]] | None' = None) -> str:
    """Build a page XHTML document.

    *content* may be either:
    - a ``list[tuple[str, str]]`` of ``(tag, text)`` pairs (structured output
      from :func:`_ocr_page_with_layout`) — tags may be ``'h2'``, ``'h3'``,
      ``'p'``, or ``'figure-img'`` (index into *inline_fig_refs*);
      a ``'p'`` with text ``'\\xa0'`` emits a blank separator.
    - a plain ``str`` (legacy: split by newlines, each line becomes a ``<p>``).

    *inline_fig_refs*: list of ``(epub_src, caption_text)`` pairs referenced by
    ``('figure-img', str_index)`` entries in *content*.
    """
    body_parts: list[str] = []
    if image_ref:
        body_parts.append(
            f'<figure>\n  <img src="{_escape_xml(image_ref)}"'
            f' alt="Page {page_num} image"/>\n</figure>'
        )
    if isinstance(content, str):
        escaped = _escape_xml(content)
        lines = escaped.split('\n')
        text_body = '\n'.join(
            f'<p>{line}</p>' if line.strip() else '<p>&#160;</p>'
            for line in lines
        )
        if content.strip():
            body_parts.append(text_body)
    else:
        text_parts: list[str] = []
        refs = inline_fig_refs or []
        for tag, text in content:
            if tag == 'figure-img':
                try:
                    idx = int(text)
                    src, caption = refs[idx]
                except (ValueError, IndexError):
                    continue
                cap_html = (
                    f'\n  <figcaption>{_escape_xml(caption)}</figcaption>'
                    if caption else ''
                )
                text_parts.append(
                    f'<figure>\n  <img src="{_escape_xml(src)}"'
                    f' alt="{_escape_xml(caption or f"Page {page_num} figure")}"/>'
                    f'{cap_html}\n</figure>'
                )
            elif text.strip() in ('', '\xa0', '\u00a0'):
                text_parts.append('<p>&#160;</p>')
            else:
                text_parts.append(f'<{tag}>{_escape_xml(text)}</{tag}>')
        text_body = '\n'.join(text_parts)
        if text_body.strip():
            body_parts.append(text_body)
    body = '\n'.join(body_parts) or '<p>&#160;</p>'
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"'
        f' xml:lang="{_escape_xml(lang)}" lang="{_escape_xml(lang)}">\n'
        '<head>\n'
        '  <meta charset="utf-8"/>\n'
        f'  <title>{_escape_xml(title)} \u2014 Page {page_num}</title>\n'
        '  <link rel="stylesheet" type="text/css" href="stylesheet.css"/>\n'
        '</head>\n'
        '<body>\n'
        f'{body}\n'
        '</body>\n'
        '</html>'
    )


def _make_opf(uid: str, title: str, lang: str, spine_items: list,
              extra_manifest: list | None = None) -> str:
    page_items = '\n    '.join(
        f'<item id="page{i + 1}" href="{f}" media-type="application/xhtml+xml"/>'
        for i, f in enumerate(spine_items)
    )
    spine_refs = '\n    '.join(
        f'<itemref idref="page{i + 1}"/>'
        for i in range(len(spine_items))
    )
    extra = '\n    '.join(extra_manifest) if extra_manifest else ''
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
        '    <item id="css" href="stylesheet.css" media-type="text/css"/>\n'
        f'    {page_items}\n'
        + (f'    {extra}\n' if extra else '') +
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

def build_epub(output_path: Path, title: str,
               page_data: 'list[tuple[list[tuple[str,str]] | str, bytes | None, list[tuple[bytes,str]]]]',
               bcp47: str) -> None:
    """Write a valid EPUB 3 archive to *output_path*.

    *page_data* is a list of ``(content, page_jpeg, inline_figs)`` tuples where:
    - *content* is a structured ``list[tuple[str,str]]`` from
      :func:`_ocr_page_with_layout` (or a plain ``str`` for legacy use).
    - *page_jpeg* is a whole-page JPEG for low-word-count pages (or ``None``).
    - *inline_figs* is a list of ``(jpeg_bytes, caption_text)`` for inline
      figures/tables detected on the page.
    """
    uid = f'book-{int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)}'
    spine_items = [f'page{i + 1}.xhtml' for i in range(len(page_data))]
    image_manifest: list[str] = []

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

        zf.writestr('OEBPS/stylesheet.css', STYLESHEET)

        for i, page_entry in enumerate(page_data):
            page_num = i + 1
            # Unpack — support legacy 2-tuple as well as new 3-tuple
            if len(page_entry) == 3:
                text, img_bytes, inline_figs = page_entry
            else:
                text, img_bytes = page_entry
                inline_figs = []

            # Write whole-page image (low-word-count pages)
            image_ref = None
            if img_bytes is not None:
                img_filename = f'images/page{page_num}.jpg'
                zf.writestr(f'OEBPS/{img_filename}', img_bytes)
                image_manifest.append(
                    f'<item id="img{page_num}" href="{img_filename}"'
                    f' media-type="image/jpeg"/>'
                )
                image_ref = f'images/page{page_num}.jpg'

            # Write inline figure/table images
            inline_fig_refs: list[tuple[str, str]] = []
            for j, (fig_bytes, caption) in enumerate(inline_figs):
                fig_filename = f'images/page{page_num}_fig{j + 1}.jpg'
                zf.writestr(f'OEBPS/{fig_filename}', fig_bytes)
                image_manifest.append(
                    f'<item id="img{page_num}f{j + 1}" href="{fig_filename}"'
                    f' media-type="image/jpeg"/>'
                )
                # src path is relative to the XHTML file in OEBPS/
                inline_fig_refs.append((fig_filename, caption))

            zf.writestr(
                f'OEBPS/page{page_num}.xhtml',
                _make_xhtml(title, page_num, text, bcp47,
                            image_ref=image_ref,
                            inline_fig_refs=inline_fig_refs),
            )

        zf.writestr('OEBPS/content.opf',
                    _make_opf(uid, title, bcp47, spine_items, image_manifest))
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
            '  python pdf_to_epub.py scan.pdf --no-images\n'
            '  python pdf_to_epub.py scan.pdf --format\n'
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
    parser.add_argument(
        '--no-images',
        action='store_true',
        help=(
            f'skip embedding page images for figure/table pages '
            f'(pages with fewer than {_FIGURE_WORD_THRESHOLD} OCR words); '
            f'produces a smaller EPUB but loses visual content'
        ),
    )
    parser.add_argument(
        '--format',
        action='store_true',
        help='run format_epub.py on the output after conversion (adds TOC and reformats headings)',
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
    embed_images = not args.no_images

    print(f'Input:    {pdf_path}')
    print(f'Output:   {output_path}')
    print(f'Title:    {title}')
    print(f'Language: {args.lang} ({bcp47})')
    print(f'Scale:    {args.scale}x')
    if embed_images:
        print(f'Images:   enabled (pages with <{_FIGURE_WORD_THRESHOLD} words get a page image)')
    else:
        print(f'Images:   disabled (--no-images)')

    # ── Load PDF ──────────────────────────────────────────────────────────────
    print('Loading PDF…')
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    print(f'Pages:    {total}')

    # ── Process each page ─────────────────────────────────────────────────────
    matrix = fitz.Matrix(args.scale, args.scale)
    page_data: list[tuple] = []
    figure_count = 0
    inline_fig_count = 0
    native_count = 0
    for i in range(total):
        page = doc[i]
        pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
        img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)

        # Choose extraction path: native text layer (digital PDFs) or OCR (scans)
        if _has_native_text(page):
            print(f'  Page {i + 1}/{total} [native]…', end='\r', flush=True)
            content, word_count, caption_figs = _extract_native_page_content(
                page, img, args.scale)
            # Native-text pages: skip _extract_embedded_images (the full-page
            # background scan is already handled via caption-based cropping above)
            all_inline_figs: list[tuple[bytes, str]] = list(caption_figs)
            native_count += 1
        else:
            print(f'  OCR page {i + 1}/{total}…', end='\r', flush=True)
            content, word_count, caption_figs = _ocr_page_with_layout(img, lang=args.lang)

            # Layer A: partial embedded images from digital PDFs
            all_inline_figs = list(caption_figs)
            if embed_images:
                embedded = _extract_embedded_images(page, img, args.scale)
                if embedded:
                    prepend: list[tuple[str, str]] = []
                    for _top_px, _bottom_px, emb_bytes in embedded:
                        idx = len(all_inline_figs)
                        all_inline_figs.append((emb_bytes, ''))
                        prepend.append(('figure-img', str(idx)))
                    content = prepend + content

        # Whole-page image for figure/table-only pages (no text, no inline figs)
        img_bytes = None
        if embed_images and word_count < _FIGURE_WORD_THRESHOLD and not all_inline_figs:
            img_bytes = _get_jpeg_bytes(img)
            figure_count += 1

        inline_fig_count += len(all_inline_figs)
        page_data.append((content, img_bytes, all_inline_figs))
    doc.close()
    ocr_count = total - native_count
    if native_count and ocr_count:
        print(f'  Done — {native_count} native-text page(s), {ocr_count} OCR page(s).          ')
    elif native_count:
        print(f'  Done — {total} page(s) extracted from native text layer.          ')
    else:
        print(f'  OCR complete — {total} page(s) processed.          ')
    if figure_count:
        print(f'  Figure pages with embedded images: {figure_count}')
    if inline_fig_count:
        print(f'  Inline figures/tables extracted:   {inline_fig_count}')

    # ── Build EPUB ────────────────────────────────────────────────────────────
    print('Building EPUB…')
    build_epub(output_path, title, page_data, bcp47)
    size_kb = output_path.stat().st_size / 1024
    print(f'Done! Saved to: {output_path} ({size_kb:.1f} KB)')

    # ── Optional: format EPUB (TOC + heading cleanup) ─────────────────────────
    if args.format:
        import importlib.util
        import shutil
        import tempfile
        fmt_script = Path(__file__).parent / 'format_epub.py'
        if not fmt_script.exists():
            print('Warning: format_epub.py not found beside pdf_to_epub.py — skipping.', file=sys.stderr)
        else:
            print('Formatting EPUB…')
            spec = importlib.util.spec_from_file_location('format_epub', fmt_script)
            fmt_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fmt_mod)
            # format_epub reads and writes; use a temp file to avoid in-place conflict
            with tempfile.NamedTemporaryFile(suffix='.epub', delete=False) as tmp:
                tmp_path = Path(tmp.name)
            shutil.move(str(output_path), str(tmp_path))
            try:
                fmt_mod.format_epub(tmp_path, output_path)
                size_kb = output_path.stat().st_size / 1024
                print(f'Formatted! Saved to: {output_path} ({size_kb:.1f} KB)')
            finally:
                tmp_path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
