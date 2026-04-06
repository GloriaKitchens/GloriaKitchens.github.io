#!/usr/bin/env python3
"""
format_epub.py – Post-process an EPUB produced by pdf_to_epub.py to improve formatting.

What it does
------------
  • Removes raw "Page N" headings inserted by the OCR step
  • Merges OCR line-per-<p> fragments back into real paragraphs
  • Promotes ALL-CAPS / PART / CHAPTER / numbered-section lines to headings
  • Applies sophisticated false-positive rejection for heading detection
  • Applies OCR character-error corrections (digit-for-letter substitutions etc.)
  • Removes standalone print page numbers (bare digits)
  • Preserves any <figure>/<img> elements already embedded in the source EPUB
  • Adds a CSS stylesheet for readable typography (including figure styles)

Requirements
------------
  Python 3.8+ standard library only — no pip install needed.

Usage
-----
    python format_epub.py book.epub
    python format_epub.py book.epub --output book_formatted.epub
"""

import argparse
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path


# ── CSS ───────────────────────────────────────────────────────────────────────

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

# ── XHTML parser: extract paragraph texts ────────────────────────────────────

class _ParagraphExtractor(HTMLParser):
    """Pull body paragraph texts and any heading / figure / img elements."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []   # raw text of each <p> (backward compat)
        self.page_h2: str | None = None   # text of first non-'Page N' <h2>
        # Ordered elements: dict with 'type' in ('p','h2','h3','figure','img')
        self.elements: list = []
        self._in_p = False
        self._in_heading: str | None = None   # current heading tag ('h2'/'h3') or None
        self._in_figure = False
        self._buf: list[str] = []
        self._figure_buf: list = []  # raw HTML fragments inside <figure>

    def _attrs_dict(self, attrs):
        return {k: v for k, v in attrs}

    def handle_starttag(self, tag, attrs):
        if tag == 'p':
            self._in_p = True
            self._buf = []
        elif tag in ('h2', 'h3'):
            self._in_heading = tag
            self._buf = []
        elif tag == 'figure':
            self._in_figure = True
            self._figure_buf = []
        elif tag == 'img':
            a = self._attrs_dict(attrs)
            src = a.get('src', '')
            alt = a.get('alt', '')
            if self._in_figure:
                self._figure_buf.append(('img', src, alt))
            else:
                # Standalone <img> outside a <figure>
                self.elements.append({'type': 'img', 'src': src, 'alt': alt})

    def handle_endtag(self, tag):
        if tag == 'p' and self._in_p:
            self._in_p = False
            text = ''.join(self._buf)
            self.paragraphs.append(text)
            self.elements.append({'type': 'p', 'text': text})
            self._buf = []
        elif tag in ('h2', 'h3') and self._in_heading == tag:
            self._in_heading = None
            text = ''.join(self._buf).strip()
            self._buf = []
            # Store first non-page-number h2 for backward compat
            if tag == 'h2' and self.page_h2 is None and not _PAGE_HEADING_RE.match(text):
                self.page_h2 = text
            self.elements.append({'type': tag, 'text': text})
        elif tag == 'figure' and self._in_figure:
            self._in_figure = False
            self.elements.append({'type': 'figure', 'children': list(self._figure_buf)})
            self._figure_buf = []

    def handle_data(self, data):
        if self._in_p or self._in_heading:
            self._buf.append(data)
        elif self._in_figure:
            stripped = data.strip()
            if stripped:
                self._figure_buf.append(('caption', stripped))


# ── Formatting transforms ─────────────────────────────────────────────────────

_NBSP = '\u00a0'   # non-breaking space (&#160;)

# ALL CAPS detection: at least 3 chars, mostly upper-case letters (allows spaces, punctuation)
_ALL_CAPS_RE = re.compile(r'^[A-Z0-9\s\.\,\:\;\-\&\/\(\)\'\"]{3,80}$')

# PART / CHAPTER headings
_PART_RE = re.compile(r'^PART\s+[IVX0-9]+', re.IGNORECASE)
_CHAP_RE = re.compile(r'^CHAPTER\s+\d+', re.IGNORECASE)

# Numbered section head: "3.1 TITLE …" — text after the number must be all-uppercase
# to avoid matching photo captions like "6. Mobile radiation monitoring: …"
_SECTION_RE = re.compile(r'^\d+\.\d*\s+[A-Z][A-Z]')

# Standalone print page number: only digits, ≤ 4 chars
_PAGE_NUM_RE = re.compile(r'^\d{1,4}$')

# Rejection helpers
# Matches compound initials like "J.A." or "A.C." (letter-period-letter or letter-period-comma)
_BIBLIO_RE  = re.compile(r'[A-Z]\.[A-Z,]')
_TABLE_RE   = re.compile(r'[A-Z]\.\s*\(\d+\)')      # table data like "R.A. (21)"
# Technical drawing/report reference codes: "ORNL-DWG 78-6264", "OTO 2954-77R", "ISBN …"
_TECH_REF_RE = re.compile(r'\bORNL\b|^ISBN\b|\b\d{1,4}-\d{3,}[A-Z]?\b|\b[A-Z]{2,4}\s+\d{4,}[-R]')
# Incomplete heading: ends with a dangling preposition/conjunction/article
_DANGLING_END_RE = re.compile(
    r'\s+(?:OF|AND|FOR|TO|IN|AN|THE|A|WITH|FROM|BY|ON|OR|AT|AS|BUT|THAT|WHICH)$'
)

# Heading generated by old pdf_to_epub.py as a page-number marker
_PAGE_HEADING_RE = re.compile(r'^\s*Page\s+\d+\s*$', re.IGNORECASE)


def _is_nbsp(text: str) -> bool:
    return text.strip() in ('', _NBSP, '\u00a0')


_FMT_IMPERATIVE_STARTERS = frozenset({
    'ATTACH', 'BEND', 'BUILD', 'CHECK', 'CONNECT', 'COVER', 'CUT', 'DO',
    'DRILL', 'FILL', 'FIT', 'FOLD', 'GLUE', 'HOLD', 'INSERT', 'KEEP',
    'MAKE', 'MARK', 'MOUNT', 'NOTE', 'PLACE', 'PULL', 'PUSH', 'PUT',
    'REMOVE', 'SCREW', 'SECURE', 'SEE', 'SET', 'SLIDE', 'TAPE', 'TIE',
    'TRIM', 'TURN', 'TWIST', 'TO', 'USE', 'WRAP', 'WARNING',
    # Safety callout keywords (not section headings)
    'CAUTION', 'NOTICE', 'DANGER',
    # Conjunction/preposition starters (fragment lines, not headings)
    'AND', 'OR', 'BUT', 'FOR', 'FROM', 'IF',
})


def _has_mixed_case_word(text: str) -> bool:
    """Return True if any word has non-standard mixed casing (OCR garbage indicator)."""
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


def _classify_heading(text: str) -> str | None:
    """Return 'h2', 'h3', or None depending on whether text looks like a heading."""
    t = text.strip()
    if not t:
        return None

    # ── Rejection rules ──────────────────────────────────────────────────────
    # 1. Starts with a quote or bracket character → footnote or OCR noise
    if t[0] in ("'", '"', '(', '[', '{'):
        return None
    # 2. Fewer than 4 alpha characters → OCR garbage (e.g. "G42", "ES.", "(O)")
    if sum(1 for c in t if c.isalpha()) < 4:
        return None
    # 3. Two or more compound-initial patterns → bibliography entry (e.g. "J.A.", "A.C.")
    if len(_BIBLIO_RE.findall(t)) >= 2:
        return None
    # 4. Two or more "Initial.(number)" patterns → table / exposure data row
    if len(_TABLE_RE.findall(t)) >= 2:
        return None
    # 5. Starts with a bare number + space (not "3.1 SECTION") → map label / noise
    if re.match(r'^\d+\s', t) and not re.match(r'^\d+\.\d', t):
        return None
    # 6. Technical drawing/report reference code → ORNL figure label, ISBN, date-codes
    if _TECH_REF_RE.search(t):
        return None
    # 7. Truncated OCR line (ends mid-word with hyphen) → not a complete heading
    if t.endswith('-'):
        return None
    # 8. Trailing comma → sentence fragment or labelled list item, not a heading
    if t.endswith(','):
        return None
    # 9. Ends with dangling preposition/conjunction → truncated OCR line, not a heading
    if _DANGLING_END_RE.search(t):
        return None
    # 10. Non-standard mixed casing within a word → upside-down / mirrored OCR garbage
    if _has_mixed_case_word(t):
        return None
    # 11. Imperative-verb or callout starters → diagram instructions, not section headings
    words = t.split()
    first_word = re.sub(r'[^A-Z]', '', words[0].upper()) if words else ''
    if first_word in _FMT_IMPERATIVE_STARTERS:
        return None
    # 12. "SECTION X-Y" diagram cross-section labels (not numeric section headings)
    if re.match(r'^SECTION\s+[A-Z]', t, re.IGNORECASE) and not re.match(r'^SECTION\s+\d', t, re.IGNORECASE):
        return None
    # 13. Unbalanced parentheses → OCR captured a fragment "(FROM...", "LEATHER (FROM"
    if t.count('(') != t.count(')'):
        return None

    # ── Positive rules ───────────────────────────────────────────────────────
    if _PART_RE.match(t) or _CHAP_RE.match(t):
        # Reject if the line is clearly a sentence (has lowercase letters beyond the
        # chapter/part number and label, e.g. "Chapter 2 examines the record…")
        # A real heading like "CHAPTER 2. THE FRAMEWORK" is short and uppercase.
        words = t.split()
        has_lowercase = any(c.islower() for c in t)
        if has_lowercase and len(words) > 6:
            return None
        return 'h2'
    if _ALL_CAPS_RE.match(t) and t != t.lower():
        return 'h2'
    if _SECTION_RE.match(t):
        return 'h3'
    return None


# ── OCR text corrections ──────────────────────────────────────────────────────

# High-confidence digit-for-letter substitutions (word-boundary anchored)
_OCR_SUBS = [
    (re.compile(r'\b1s\b'),  'is'),
    (re.compile(r'\b1t\b'),  'it'),
    (re.compile(r'\b1n\b'),  'in'),
    (re.compile(r'\b0f\b'),  'of'),
    (re.compile(r'\b1f\b'),  'if'),
    (re.compile(r'\[AEA\b'), 'IAEA'),
]
_MULTI_SPACE_RE = re.compile(r' {2,}')
_SPACE_BEFORE_PUNCT_RE = re.compile(r'\s+([,;:])')


def _fix_ocr_text(text: str) -> str:
    """Apply high-confidence OCR character-error corrections to a merged paragraph."""
    for pattern, replacement in _OCR_SUBS:
        text = pattern.sub(replacement, text)
    text = _MULTI_SPACE_RE.sub(' ', text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r'\1', text)
    return text


def _merge_lines(lines: list[str]) -> str:
    """Join a list of non-empty lines into one string, handling soft hyphens."""
    parts: list[str] = []
    for line in lines:
        if not line:
            continue
        if parts and parts[-1].endswith('-'):
            parts[-1] = parts[-1][:-1] + line
        elif parts:
            parts.append(' ' + line)
        else:
            parts.append(line)
    return ''.join(parts).strip()


def _merge_and_format(raw_paragraphs: list[str]) -> list[tuple[str, str]]:
    """
    Given the list of raw <p> texts from one XHTML page, return a list of
    (tag, text) tuples representing the formatted output elements.

    Strategy
    --------
    1. Split into "blocks" using &#160; (nbsp) paragraphs as delimiters.
    2. Within each block, if the first line qualifies as a heading, emit it as
       a standalone heading and emit any remaining lines as a separate paragraph.
    3. Otherwise merge all lines into one <p>, handling soft hyphens.
    4. Discard standalone print page numbers.
    """
    # Split into blocks separated by nbsp lines
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in raw_paragraphs:
        if _is_nbsp(raw):
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(raw.strip())
    if current:
        blocks.append(current)

    result: list[tuple[str, str]] = []

    for block in blocks:
        lines = [l for l in block if l]  # drop empty strings
        if not lines:
            continue

        first = lines[0]
        heading_tag = _classify_heading(first)

        if heading_tag:
            # Emit the heading line separately
            result.append((heading_tag, first))
            # Merge remaining lines as a paragraph (if any)
            rest = lines[1:]
            if rest:
                merged = _fix_ocr_text(_merge_lines(rest))
                if merged and not _PAGE_NUM_RE.match(merged):
                    result.append(('p', merged))
        else:
            merged = _fix_ocr_text(_merge_lines(lines))
            if not merged:
                continue
            if _PAGE_NUM_RE.match(merged):
                continue
            result.append(('p', merged))

    return result


def _process_elements(elements: list[dict],
                      promote_paragraphs: bool = True) -> list[tuple[str, object]]:
    """Process a mixed element list from :class:`_ParagraphExtractor`.

    Returns an ordered list of ``(tag, content)`` tuples:

    - ``('h2'/'h3', text_str)`` — heading
    - ``('p', text_str)`` — paragraph
    - ``('figure', elem_dict)`` — figure/img (pass-through)

    Layer 2 — source-tag preservation
        Elements already tagged ``<h2>`` or ``<h3>`` in the source EPUB (i.e.
        emitted by the font-aware :func:`_ocr_page_with_layout`) are passed
        through directly without re-running :func:`_classify_heading`.
        ``<p>`` elements are classified with the pattern-based logic only when
        *promote_paragraphs* is True (for old-style EPUBs with no source headings).
        When False (new-style EPUBs whose headings are already tagged), paragraphs
        are kept as-is to avoid false promotions from diagram labels.

    Layer 3 — contextual isolation
        If two consecutive heading elements (tag ``h2``) appear with no body
        paragraph between them, and neither is a CHAPTER / PART / TABLE match,
        the second heading is demoted to ``<p>``.  This prevents dense runs of
        ALL-CAPS labels from assembly-instruction pages flooding the TOC.
    """
    def _is_structural(text: str) -> bool:
        """Return True for CHAPTER / PART / TABLE headers that may legitimately stack."""
        t = text.strip().upper()
        return bool(
            _PART_RE.match(text) or
            _CHAP_RE.match(text) or
            t.startswith('TABLE')
        )

    result: list[tuple[str, object]] = []
    p_buffer: list[str] = []   # accumulated <p> texts for merging
    prev_heading: bool = False  # was the last emitted element a heading?

    def flush_p() -> None:
        nonlocal p_buffer, prev_heading
        if not p_buffer:
            return
        if promote_paragraphs:
            formatted = _merge_and_format(p_buffer)
        else:
            # Source EPUB already has semantic headings — just merge as body text
            merged = _fix_ocr_text(_merge_lines([l for l in p_buffer if not _is_nbsp(l)]))
            formatted = [('p', merged)] if merged and not _PAGE_NUM_RE.match(merged) else []
        for ftag, ftext in formatted:
            if ftag in ('h2', 'h3'):
                # Context isolation: demote all h2s in a run with no body between.
                # Do NOT reset prev_heading when demoting — this ensures all h2s
                # after the first in a consecutive run are also demoted.
                if prev_heading and ftag == 'h2' and not _is_structural(ftext):
                    result.append(('p', ftext))
                    # prev_heading stays True — next h2 is also part of the run
                else:
                    result.append((ftag, ftext))
                    prev_heading = True
            else:
                result.append((ftag, ftext))
                prev_heading = False
        p_buffer.clear()

    for elem in elements:
        etype = elem['type']
        if etype == 'p':
            p_buffer.append(elem['text'])

        elif etype in ('h2', 'h3'):
            flush_p()
            text = elem['text'].strip()
            # Filter "Page N" artifacts from old-format EPUBs
            if _PAGE_HEADING_RE.match(text):
                continue
            # Skip empty headings
            if not text:
                continue
            # Quality gate: apply classification filter to source-tagged headings
            # to remove OCR garbage that pdf_to_epub.py incorrectly promoted.
            # Uses same criteria as _classify_heading() — allows Chapter/Appendix
            # structural patterns and well-formed ALL-CAPS headings; rejects mixed-case
            # OCR garbage, imperative callouts, and symbol-heavy lines.
            if _classify_heading(text) is None:
                # Demote to paragraph rather than discard — content is preserved
                p_buffer.append(text)
                continue
            # Context isolation for source-tagged headings — same rule: all h2s
            # after the first in a run (no body text between) are demoted.
            if prev_heading and etype == 'h2' and not _is_structural(text):
                result.append(('p', text))
                # prev_heading stays True — don't reset so next h2 is also demoted
            else:
                result.append((etype, text))
                prev_heading = True

        elif etype in ('figure', 'img'):
            flush_p()
            result.append(('figure', elem))
            prev_heading = False

    flush_p()  # flush any trailing <p> buffer
    return result


# ── XHTML builder ─────────────────────────────────────────────────────────────

def _escape_xml(text: str) -> str:
    return (
        str(text)
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&apos;')
    )


def _render_figure(fig: dict) -> str:
    """Render a figure dict (from _ParagraphExtractor) back to XHTML."""
    parts = ['<figure>']
    caption = None
    for item in fig.get('children', []):
        kind = item[0]
        if kind == 'img':
            _, src, alt = item
            parts.append(f'  <img src="{_escape_xml(src)}" alt="{_escape_xml(alt)}"/>')
        elif kind == 'caption':
            caption = item[1]
    if caption:
        parts.append(f'  <figcaption>{_escape_xml(caption)}</figcaption>')
    parts.append('</figure>')
    return '\n'.join(parts)


def _reformat_xhtml(source: str, promote_paragraphs: bool = True) -> tuple[str, str | None]:
    """Parse a page XHTML, apply formatting transforms.

    Returns (reformatted_xhtml, first_heading_text_or_None).
    Preserves any <figure>/<img> elements already present in the source.
    Source heading tags (<h2>/<h3>) are preserved as-is (layout-based headings
    from the new pdf_to_epub.py pass-through without re-classification).

    When *promote_paragraphs* is False (new-style EPUB with font-size headings),
    <p> elements are not re-classified as headings — this prevents diagram labels
    from being promoted on pages where the OCR converter didn't detect headings.
    """
    parser = _ParagraphExtractor()
    parser.feed(source)

    # Extract metadata from the existing <html> tag (lang, title)
    lang_match = re.search(r'xml:lang="([^"]*)"', source)
    lang = lang_match.group(1) if lang_match else 'en'

    title_match = re.search(r'<title>([^<]*)</title>', source)
    title = title_match.group(1) if title_match else ''

    processed = _process_elements(parser.elements, promote_paragraphs=promote_paragraphs)

    # Extract first heading for TOC use
    first_heading = next(
        (content for tag, content in processed
         if isinstance(content, str) and tag in ('h2', 'h3')),
        None,
    )

    # Build body
    body_lines: list[str] = []
    for tag, content in processed:
        if tag == 'figure':
            elem = content
            if elem['type'] == 'figure':
                body_lines.append(_render_figure(elem))
            elif elem['type'] == 'img':
                src = _escape_xml(elem['src'])
                alt = _escape_xml(elem['alt'])
                body_lines.append(f'<figure>\n  <img src="{src}" alt="{alt}"/>\n</figure>')
        else:
            body_lines.append(f'<{tag}>{_escape_xml(content)}</{tag}>')

    body = '\n'.join(body_lines) or '<p>&#160;</p>'

    xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"'
        f' xml:lang="{_escape_xml(lang)}" lang="{_escape_xml(lang)}">\n'
        '<head>\n'
        '  <meta charset="utf-8"/>\n'
        f'  <title>{_escape_xml(title)}</title>\n'
        '  <link rel="stylesheet" type="text/css" href="stylesheet.css"/>\n'
        '</head>\n'
        '<body>\n'
        f'{body}\n'
        '</body>\n'
        '</html>'
    )
    return xhtml, first_heading


# ── Nav / NCX builders ────────────────────────────────────────────────────────

def _make_nav(title: str, spine_items: list[str], toc_labels: list[str], lang: str) -> str:
    items = '\n      '.join(
        f'<li><a href="{f}">{_escape_xml(label)}</a></li>'
        for f, label in zip(spine_items, toc_labels)
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


def _make_ncx(uid: str, title: str, spine_items: list[str], toc_labels: list[str]) -> str:
    nav_points = '\n  '.join(
        f'<navPoint id="np{i + 1}" playOrder="{i + 1}">'
        f'<navLabel><text>{_escape_xml(label)}</text></navLabel>'
        f'<content src="{f}"/>'
        f'</navPoint>'
        for i, (f, label) in enumerate(zip(spine_items, toc_labels))
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


# ── OPF manifest patcher ──────────────────────────────────────────────────────

_CSS_MANIFEST_ITEM = (
    '<item id="css" href="stylesheet.css" media-type="text/css"/>'
)

def _patch_opf(opf_source: str) -> str:
    """Add stylesheet.css to the OPF manifest if not already present."""
    if 'stylesheet.css' in opf_source:
        return opf_source
    # Insert before the closing </manifest>
    return opf_source.replace('</manifest>', f'    {_CSS_MANIFEST_ITEM}\n  </manifest>', 1)


# ── Core ──────────────────────────────────────────────────────────────────────

def format_epub(input_path: Path, output_path: Path) -> None:
    """Read *input_path*, apply formatting, write to *output_path*."""
    with zipfile.ZipFile(input_path, 'r') as zin:
        names = zin.namelist()

        # ── Pass 1: reformat all pages, collecting TOC labels ──────────────
        # Sort page files numerically so labels align with spine order
        page_names = sorted(
            (n for n in names if re.match(r'OEBPS/page\d+\.xhtml$', n)),
            key=lambda n: int(re.search(r'\d+', n).group()),
        )

        page_results: dict[str, tuple[str, str | None]] = {}  # name → (xhtml, heading)

        # Detect whether this is a new-style EPUB (pdf_to_epub.py font-size analysis):
        # scan a sample of pages for real <h2>/<h3> elements (not "Page N" markers).
        # New-style EPUBs have semantic h2/h3 from height-ratio detection; old-style
        # have only "Page N" h2 markers and rely entirely on _classify_heading() for
        # heading detection from <p> text. For both styles, <p> promotion is enabled
        # — new-style EPUBs benefit because height-ratio sometimes misses real headings.
        # Source h2/h3 tags in new-style EPUBs are filtered through _classify_heading()
        # quality gate inside _process_elements() to remove OCR garbage.
        for name in page_names:
            text = zin.read(name).decode('utf-8')
            page_results[name] = _reformat_xhtml(text)

        # Build spine item list and TOC entries — skip pages with no heading
        # (assumed to be front matter, blank pages, or index/appendix content)
        spine_items = [re.search(r'page\d+\.xhtml', n).group() for n in page_names]
        toc_entries: list[tuple[str, str]] = [
            (re.search(r'page\d+\.xhtml', n).group(), page_results[n][1])
            for n in page_names
            if page_results[n][1] is not None
        ]

        # TOC deduplication (Layer 4): heading text appearing 3+ times is a
        # repeated label (e.g. appendix sub-headings).  Keep the first
        # occurrence in the nav; suppress subsequent ones to reduce TOC noise.
        # The h2 in the page body is always preserved regardless.
        from collections import Counter
        _label_counts = Counter(label for _, label in toc_entries)
        _seen_labels: set[str] = set()
        deduped: list[tuple[str, str]] = []
        for _f, _label in toc_entries:
            if _label_counts[_label] >= 3:
                if _label in _seen_labels:
                    continue
                _seen_labels.add(_label)
            deduped.append((_f, _label))
        toc_entries = deduped

        toc_files  = [f for f, _ in toc_entries]
        toc_labels = [label for _, label in toc_entries]

        # Read nav lang from first page (or fall back to 'en')
        nav_lang = 'en'
        if page_names:
            first_src = zin.read(page_names[0]).decode('utf-8')
            m = re.search(r'xml:lang="([^"]*)"', first_src)
            if m:
                nav_lang = m.group(1)

        # Extract uid and book title from OPF for NCX regeneration
        uid = 'book-reformatted'
        book_title = output_path.stem
        opf_src = None
        if 'OEBPS/content.opf' in names:
            opf_src = zin.read('OEBPS/content.opf').decode('utf-8')
            uid_m = re.search(r'<dc:identifier[^>]*>([^<]+)</dc:identifier>', opf_src)
            if uid_m:
                uid = uid_m.group(1)
            title_m = re.search(r'<dc:title>([^<]+)</dc:title>', opf_src)
            if title_m:
                book_title = title_m.group(1)

        # ── Pass 2: write output archive ───────────────────────────────────
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zout:

            for name in names:
                data = zin.read(name)

                if name == 'mimetype':
                    info = zipfile.ZipInfo('mimetype')
                    info.compress_type = zipfile.ZIP_STORED
                    zout.writestr(info, data)
                    continue

                if name in page_results:
                    reformatted_xhtml, _ = page_results[name]
                    zout.writestr(name, reformatted_xhtml.encode('utf-8'))
                    continue

                if name == 'OEBPS/content.opf' and opf_src is not None:
                    zout.writestr(name, _patch_opf(opf_src).encode('utf-8'))
                    continue

                if name == 'OEBPS/nav.xhtml':
                    nav = _make_nav(book_title, toc_files, toc_labels, nav_lang)
                    zout.writestr(name, nav.encode('utf-8'))
                    continue

                if name == 'OEBPS/toc.ncx':
                    ncx = _make_ncx(uid, book_title, toc_files, toc_labels)
                    zout.writestr(name, ncx.encode('utf-8'))
                    continue

                zout.writestr(name, data)

            # Add stylesheet if not already present
            if 'OEBPS/stylesheet.css' not in names:
                zout.writestr('OEBPS/stylesheet.css', STYLESHEET.encode('utf-8'))

    size_kb = output_path.stat().st_size / 1024
    print(f'Done! Saved to: {output_path} ({size_kb:.1f} KB)')


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog='format_epub.py',
        description='Improve the formatting of an EPUB produced by pdf_to_epub.py.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'examples:\n'
            '  python format_epub.py book.epub\n'
            '  python format_epub.py book.epub --output book_formatted.epub\n'
        ),
    )
    parser.add_argument('epub', help='path to the input EPUB file')
    parser.add_argument(
        '--output', '-o',
        help='output EPUB path (default: <input>_formatted.epub)',
    )
    args = parser.parse_args()

    input_path = Path(args.epub)
    if not input_path.exists():
        sys.exit(f'Error: file not found: {input_path}')
    if input_path.suffix.lower() != '.epub':
        print(f'Warning: {input_path.name} does not have a .epub extension', file=sys.stderr)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_stem(input_path.stem + '_formatted')

    print(f'Input:  {input_path}')
    print(f'Output: {output_path}')
    print('Reformatting…')
    format_epub(input_path, output_path)


if __name__ == '__main__':
    main()
