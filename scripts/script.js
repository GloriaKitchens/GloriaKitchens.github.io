/* ─────────────────────────────────────────
   PDF → EPUB Converter  (client-side only)
   Uses:  PDF.js · Tesseract.js · JSZip
───────────────────────────────────────── */

(function () {
  'use strict';

  // ── DOM refs ──────────────────────────────
  const dropZone       = document.getElementById('dropZone');
  const fileInput      = document.getElementById('fileInput');
  const fileNameEl     = document.getElementById('fileName');
  const langSelect     = document.getElementById('langSelect');
  const bookTitleInput = document.getElementById('bookTitle');
  const convertBtn     = document.getElementById('convertBtn');
  const progressSection = document.getElementById('progressSection');
  const progressFill   = document.getElementById('progressFill');
  const progressLabel  = document.getElementById('progressLabel');
  const resultSection  = document.getElementById('resultSection');
  const downloadLink   = document.getElementById('downloadLink');
  const errorSection   = document.getElementById('errorSection');
  const errorMsg       = document.getElementById('errorMsg');
  const debugLogEl     = document.getElementById('debugLog');

  let selectedFile    = null;
  let currentObjectUrl = null;
  let debugLines      = []; // buffer for debug log entries

  // ── PDF.js worker ─────────────────────────
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  // ── File selection ────────────────────────
  fileInput.addEventListener('change', () => {
    const f = fileInput.files[0];
    if (f && !looksLikePdf(f)) {
      showError('Please select a valid PDF file.');
      fileInput.value = '';
      return;
    }
    handleFile(f);
  });

  dropZone.addEventListener('click', (e) => {
    if (e.target.closest('label')) return; // let the label's native for= handle it
    fileInput.click();
  });

  dropZone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInput.click();
    }
  });

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-over');
  });

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const f = e.dataTransfer.files[0];
    if (f && looksLikePdf(f)) {
      handleFile(f);
    } else {
      showError('Please drop a valid PDF file.');
    }
  });

  function handleFile(file) {
    if (!file) return;
    selectedFile = file;
    fileNameEl.textContent = file.name;
    convertBtn.disabled = false;
    hideResults();
    debugLog(`File selected: ${file.name} (${formatBytes(file.size)}, type: ${file.type || 'unknown'})`);
  }

  // ── Convert ───────────────────────────────
  convertBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    hideResults();
    clearDebugLog();
    convertBtn.disabled = true;
    showProgress(0, 'Reading file…');

    try {
      const title    = bookTitleInput.value.trim() || selectedFile.name.replace(/\.pdf$/i, '') || 'My Book';
      const tessLang = langSelect.value;
      debugLog(`Starting conversion — title: "${title}", language: ${tessLang}`);

      // Map Tesseract lang code → BCP 47 tag (used for EPUB metadata & XHTML xml:lang)
      const langMap = {
        eng: 'en', fra: 'fr', deu: 'de', spa: 'es', ita: 'it',
        por: 'pt', rus: 'ru', chi_sim: 'zh-Hans', jpn: 'ja', ara: 'ar'
      };
      const bcp47 = langMap[tessLang] || tessLang;

      // 1. Read file with progress (0 – 10 %)
      debugLog('Reading file from disk…');
      const arrayBuffer = await readFileAsArrayBuffer(selectedFile);
      debugLog(`File read complete (${formatBytes(arrayBuffer.byteLength)})`);

      // 2. Load PDF
      showProgress(10, 'Loading PDF…');
      debugLog('Loading PDF with PDF.js…');
      const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;
      const totalPages = pdf.numPages;
      debugLog(`PDF loaded — ${totalPages} page(s) found`);

      // 3. OCR each page (10 – 85 %)
      debugLog(`Initialising Tesseract OCR (language: ${tessLang})…`);
      let lastTesseractStatus = null;
      const worker = await Tesseract.createWorker(tessLang, 1, {
        logger: (m) => {
          if (m.status && m.status !== lastTesseractStatus) {
            lastTesseractStatus = m.status;
            debugLog(`  Tesseract: ${m.status}`);
          }
        }
      });
      debugLog('OCR worker ready');

      const pageTexts = [];
      for (let i = 1; i <= totalPages; i++) {
        const pct = Math.round(10 + ((i - 1) / totalPages) * 75);
        showProgress(pct, `Recognising page ${i} of ${totalPages}…`);
        debugLog(`OCR page ${i} / ${totalPages}…`);
        lastTesseractStatus = null; // reset so status changes log per page

        const canvas = await renderPageToCanvas(pdf, i);
        const { data: { text } } = await worker.recognize(canvas);
        pageTexts.push(text.trim());
        debugLog(`  → page ${i} done (${text.trim().length} characters extracted)`);
      }

      await worker.terminate();
      debugLog('OCR worker terminated');

      // 4. Build EPUB
      showProgress(90, 'Building EPUB…');
      debugLog('Building EPUB archive…');
      const epubBlob = await buildEpub(title, pageTexts, bcp47);
      debugLog(`EPUB built (${formatBytes(epubBlob.size)})`);

      // 5. Offer download – revoke any previous URL to free memory
      showProgress(100, 'Done!');
      debugLog('✅ Conversion complete!');
      if (currentObjectUrl) URL.revokeObjectURL(currentObjectUrl);
      currentObjectUrl = URL.createObjectURL(epubBlob);
      downloadLink.href = currentObjectUrl;
      downloadLink.download = sanitizeFilename(title) + '.epub';
      showResult();
    } catch (err) {
      debugLog(`❌ Error: ${err.message || err}`);
      showError('Something went wrong: ' + (err.message || err));
      convertBtn.disabled = false;
    }
  });

  // ── Read file with progress events ────────
  function readFileAsArrayBuffer(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      let lastBarPct = -1;
      let lastLoggedMilestone = -1;
      reader.onprogress = (e) => {
        if (e.lengthComputable) {
          const filePct  = Math.floor((e.loaded / e.total) * 100);
          const barPct   = Math.floor((e.loaded / e.total) * 10); // maps read to 0-10% of bar
          if (barPct !== lastBarPct) {
            lastBarPct = barPct;
            showProgress(barPct, `Reading file… ${filePct}%`);
          }
          // Log at 25 % intervals, triggering as soon as the threshold is crossed
          const milestone = Math.floor(filePct / 25) * 25;
          if (milestone > 0 && milestone > lastLoggedMilestone) {
            lastLoggedMilestone = milestone;
            debugLog(`  Reading file: ${milestone}%`);
          }
        }
      };
      reader.onload  = (e) => {
        // Guarantee the 100% milestone is always logged/shown even if onprogress
        // didn't fire at exactly 100%.
        if (lastLoggedMilestone < 100) {
          lastLoggedMilestone = 100;
          debugLog('  Reading file: 100%');
          showProgress(10, 'Reading file… 100%');
        }
        resolve(e.target.result);
      };
      reader.onerror = ()  => reject(new Error('Failed to read file'));
      reader.readAsArrayBuffer(file);
    });
  }

  // ── Render a PDF page to an HTMLCanvasElement ──
  async function renderPageToCanvas(pdf, pageNumber) {
    const page     = await pdf.getPage(pageNumber);
    const viewport = page.getViewport({ scale: 2.0 }); // higher scale → better OCR
    const canvas   = document.createElement('canvas');
    canvas.width   = viewport.width;
    canvas.height  = viewport.height;
    const ctx      = canvas.getContext('2d');
    await page.render({ canvasContext: ctx, viewport }).promise;
    return canvas;
  }

  // ── EPUB stylesheet ───────────────────────────
  const EPUB_STYLESHEET = `body {
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
`;

  // ── Formatting helpers (mirrors format_epub.py logic) ──
  const ALL_CAPS_RE   = /^[A-Z0-9\s.,;:\-&/()'"]{3,80}$/;
  const PART_RE       = /^PART\s+[IVX0-9]+/i;
  const CHAPTER_RE    = /^CHAPTER\s+\d+/i;
  // Section RE: text after the number must begin with two uppercase chars
  // to avoid matching photo captions like "6. Mobile radiation monitoring: …"
  const SECTION_RE    = /^\d+\.\d*\s+[A-Z][A-Z]/;
  const PAGE_NUM_RE   = /^\d{1,4}$/;
  const NBSP          = '\u00a0';

  // Rejection helpers
  // Matches compound initials like "J.A." or "A.C." (letter-period-letter or letter-period-comma)
  const BIBLIO_RE = /[A-Z]\.[A-Z,]/g;
  const TABLE_RE  = /[A-Z]\.\s*\(\d+\)/g;     // table data like "R.A. (21)"
  // Technical drawing/report reference codes: ORNL-DWG 78-6264, OTO 2954-77R, ISBN …
  const TECH_REF_RE = /\bORNL\b|^ISBN\b|\b\d{1,4}-\d{3,}[A-Z]?\b|\b[A-Z]{2,4}\s+\d{4,}[-R]/;
  // Incomplete heading: ends with a dangling preposition/conjunction
  const DANGLING_END_RE = /\s+(?:OF|AND|FOR|TO|IN|AN|THE|A|WITH|FROM|BY|ON|OR|AT|AS|BUT|THAT|WHICH)$/;
  // Safety callouts, imperative verbs, and conjunction/preposition starters
  const IMPERATIVE_STARTERS = new Set([
    'ATTACH', 'BEND', 'BUILD', 'CHECK', 'CONNECT', 'COVER', 'CUT', 'DO',
    'DRILL', 'FILL', 'FIT', 'FOLD', 'GLUE', 'HOLD', 'INSERT', 'KEEP',
    'MAKE', 'MARK', 'MOUNT', 'NOTE', 'PLACE', 'PULL', 'PUSH', 'PUT',
    'REMOVE', 'SCREW', 'SECURE', 'SEE', 'SET', 'SLIDE', 'TAPE', 'TIE',
    'TRIM', 'TURN', 'TWIST', 'TO', 'USE', 'WRAP', 'WARNING',
    'CAUTION', 'NOTICE', 'DANGER',
    'AND', 'OR', 'BUT', 'FOR', 'FROM', 'IF',
  ]);

  function isNbspLine(text) {
    const t = text.trim();
    return t === '' || t === NBSP || t === '\u00a0';
  }

  // Returns true if any word has non-standard mixed casing (OCR garbage indicator)
  function hasMixedCaseWord(text) {
    return text.split(/\s+/).some(word => {
      const alpha = word.replace(/[^a-zA-Z]/g, '');
      if (alpha.length < 3) return false;
      const allUpper = alpha === alpha.toUpperCase();
      const allLower = alpha === alpha.toLowerCase();
      const titleCase = alpha[0] === alpha[0].toUpperCase() &&
                        alpha.slice(1) === alpha.slice(1).toLowerCase();
      return !(allUpper || allLower || titleCase);
    });
  }

  function classifyHeading(text) {
    const t = text.trim();
    if (!t) return null;

    // Rejection rules
    // 1. Starts with quote/bracket → footnote or OCR noise
    if ("'\"([{".includes(t[0])) return null;
    // 2. Fewer than 4 alpha characters → OCR garbage
    if ((t.match(/[a-zA-Z]/g) || []).length < 4) return null;
    // 3. Two+ compound initials → bibliography entry
    if ((t.match(BIBLIO_RE) || []).length >= 2) return null;
    // 4. Two+ "Initial.(number)" patterns → table/exposure data row
    if ((t.match(TABLE_RE)  || []).length >= 2) return null;
    // 5. Digit-start that isn't a section number (3.1, 42.) → OCR noise or reversed text
    if (/^\d/.test(t) && !/^\d+\./.test(t)) return null;
    // 6. Technical drawing/report reference codes → figure labels, ISBN, date codes
    if (TECH_REF_RE.test(t)) return null;
    // 7. Truncated OCR line ending mid-word
    if (t.endsWith('-')) return null;
    // 8. Trailing comma → sentence fragment
    if (t.endsWith(',')) return null;
    // 9. Ends with dangling preposition/conjunction → truncated OCR line
    if (DANGLING_END_RE.test(t)) return null;
    // 10. Non-standard mixed casing → upside-down/mirrored OCR garbage
    if (hasMixedCaseWord(t)) return null;
    // 11. Imperative-verb, callout, or conjunction/preposition starters
    const firstWord = t.split(/\s+/)[0].replace(/[^A-Za-z]/g, '').toUpperCase();
    if (IMPERATIVE_STARTERS.has(firstWord)) return null;
    // 12. "SECTION X-Y" cross-section labels (not numeric section headings)
    if (/^SECTION\s+[A-Z]/i.test(t) && !/^SECTION\s+\d/i.test(t)) return null;
    // 13. Unbalanced parentheses → OCR captured a fragment
    if ((t.match(/\(/g) || []).length !== (t.match(/\)/g) || []).length) return null;

    if (PART_RE.test(t) || CHAPTER_RE.test(t)) {
      // Reject if the line is a sentence: has lowercase beyond the chapter label
      // and is long (e.g. "Chapter 2 examines the record-keeping practices…")
      const hasLower = /[a-z]/.test(t);
      if (hasLower && t.split(/\s+/).length > 6) return null;
      return 'h2';
    }
    if (ALL_CAPS_RE.test(t) && t !== t.toLowerCase()) return 'h2';
    if (SECTION_RE.test(t)) return 'h3';
    return null;
  }

  // OCR corrections: digit-for-letter, bracket confusion, whitespace cleanup
  const OCR_SUBS = [
    [/\b1s\b/g,  'is'],
    [/\b1t\b/g,  'it'],
    [/\b1n\b/g,  'in'],
    [/\b0f\b/g,  'of'],
    [/\b1f\b/g,  'if'],
    [/\[AEA\b/g, 'IAEA'],
  ];
  function fixOcrText(text) {
    for (const [pattern, replacement] of OCR_SUBS) {
      text = text.replace(pattern, replacement);
    }
    text = text.replace(/ {2,}/g, ' ');
    text = text.replace(/\s+([,;:])/g, '$1');
    return text;
  }

  function mergeLines(lines) {
    const parts = [];
    for (const line of lines) {
      if (!line) continue;
      if (parts.length && parts[parts.length - 1].endsWith('-')) {
        parts[parts.length - 1] = parts[parts.length - 1].slice(0, -1) + line;
      } else if (parts.length) {
        parts.push(' ' + line);
      } else {
        parts.push(line);
      }
    }
    return parts.join('').trim();
  }

  function formatPageLines(rawLines) {
    // Split into blocks using nbsp lines as delimiters
    const blocks = [];
    let current = [];
    for (const raw of rawLines) {
      if (isNbspLine(raw)) {
        if (current.length) { blocks.push(current); current = []; }
      } else {
        current.push(raw.trim());
      }
    }
    if (current.length) blocks.push(current);

    const result = []; // [{tag, text}]
    for (const block of blocks) {
      const lines = block.filter(l => l.length > 0);
      if (!lines.length) continue;

      const headingTag = classifyHeading(lines[0]);
      if (headingTag) {
        result.push({ tag: headingTag, text: lines[0] });
        const rest = lines.slice(1);
        if (rest.length) {
          const merged = fixOcrText(mergeLines(rest));
          if (merged && !PAGE_NUM_RE.test(merged)) {
            result.push({ tag: 'p', text: merged });
          }
        }
      } else {
        const merged = fixOcrText(mergeLines(lines));
        if (!merged || PAGE_NUM_RE.test(merged)) continue;
        result.push({ tag: 'p', text: merged });
      }
    }

    // Context isolation: demote all h2s after the first in a consecutive run
    // (no body paragraph between them). Mirrors format_epub.py flush_p() logic.
    const isolated = [];
    let prevWasHeading = false;
    for (const el of result) {
      if (el.tag === 'h2') {
        const isStructural = PART_RE.test(el.text) || CHAPTER_RE.test(el.text) ||
                             /^TABLE\b/i.test(el.text);
        if (prevWasHeading && !isStructural) {
          isolated.push({ tag: 'p', text: el.text }); // demote; prevWasHeading stays true
        } else {
          isolated.push(el);
          prevWasHeading = true;
        }
      } else {
        isolated.push(el);
        prevWasHeading = (el.tag === 'h3'); // body text resets; h3 counts as a heading
      }
    }
    return isolated;
  }

  // ── Build a minimal valid EPUB 3 ─────────────
  function buildEpub(title, pageTexts, lang) {
    const zip  = new JSZip();
    const id   = 'book-' + Date.now();

    // mimetype (must be first and uncompressed)
    zip.file('mimetype', 'application/epub+zip', { compression: 'STORE' });

    // META-INF/container.xml
    zip.folder('META-INF').file('container.xml',
      '<?xml version="1.0"?>' +
      '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">' +
        '<rootfiles>' +
          '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>' +
        '</rootfiles>' +
      '</container>'
    );

    const oebps = zip.folder('OEBPS');

    // Stylesheet
    oebps.file('stylesheet.css', EPUB_STYLESHEET);

    // Format each page, collect spine items and TOC entries (only pages with a heading)
    const spineItems = [];
    const tocEntries = []; // {file, label}
    pageTexts.forEach((text, i) => {
      const fname = `page${i + 1}.xhtml`;
      const rawLines  = text.split('\n').map(l => l.trim());
      const formatted = formatPageLines(rawLines);
      const firstHeading = formatted.find(el => el.tag === 'h2' || el.tag === 'h3');
      // Only add to TOC if a heading was detected; headingless pages are
      // assumed to be front matter, blank pages, or index/appendix content.
      if (firstHeading) tocEntries.push({ file: fname, label: firstHeading.text });
      oebps.file(fname, makeXhtmlFromFormatted(title, i + 1, formatted, lang));
      spineItems.push(fname);
    });

    // TOC deduplication: heading text appearing 3+ times is a repeated label
    // (e.g. appendix sub-headings). Keep only the first occurrence in the nav.
    const labelCounts = {};
    for (const { label } of tocEntries) {
      labelCounts[label] = (labelCounts[label] || 0) + 1;
    }
    const seenLabels = new Set();
    const deduped = tocEntries.filter(({ label }) => {
      if (labelCounts[label] >= 3) {
        if (seenLabels.has(label)) return false;
        seenLabels.add(label);
      }
      return true;
    });
    const tocFiles  = deduped.map(e => e.file);
    const tocLabels = deduped.map(e => e.label);

    // content.opf  (package document)
    oebps.file('content.opf', makeOpf(id, title, lang, spineItems));

    // toc.ncx  (legacy nav for older readers)
    oebps.file('toc.ncx', makeNcx(id, title, tocFiles, tocLabels));

    // nav.xhtml  (EPUB 3 nav document)
    oebps.file('nav.xhtml', makeNav(title, tocFiles, tocLabels, lang));

    return zip.generateAsync({ type: 'blob', mimeType: 'application/epub+zip' });
  }

  function makeXhtmlFromFormatted(title, pageNum, formatted, lang) {
    const body = formatted
      .map(({ tag, text: t }) => `<${tag}>${escapeXml(t)}</${tag}>`)
      .join('\n') || '<p>&#160;</p>';
    return `<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="${escapeXml(lang)}" lang="${escapeXml(lang)}">
<head>
  <meta charset="utf-8"/>
  <title>${escapeXml(title)} — Page ${pageNum}</title>
  <link rel="stylesheet" type="text/css" href="stylesheet.css"/>
</head>
<body>
${body}
</body>
</html>`;
  }

  function makeXhtml(title, pageNum, text, lang) {
    const rawLines  = text.split('\n').map(l => l.trim());
    const formatted = formatPageLines(rawLines);
    return makeXhtmlFromFormatted(title, pageNum, formatted, lang);
  }

  function makeOpf(id, title, lang, spineItems) {
    const manifestItems = spineItems.map((f, i) =>
      `<item id="page${i + 1}" href="${f}" media-type="application/xhtml+xml"/>`
    ).join('\n    ');
    const navItem =
      '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>';
    const ncxItem =
      '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>';
    const cssItem =
      '<item id="css" href="stylesheet.css" media-type="text/css"/>';
    const spineRefs = spineItems.map((_, i) =>
      `<itemref idref="page${i + 1}"/>`
    ).join('\n    ');

    return `<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">${escapeXml(id)}</dc:identifier>
    <dc:title>${escapeXml(title)}</dc:title>
    <dc:language>${escapeXml(lang)}</dc:language>
    <meta property="dcterms:modified">${new Date().toISOString().replace(/\.\d+Z$/, 'Z')}</meta>
  </metadata>
  <manifest>
    ${navItem}
    ${ncxItem}
    ${cssItem}
    ${manifestItems}
  </manifest>
  <spine toc="ncx">
    ${spineRefs}
  </spine>
</package>`;
  }

  function makeNcx(id, title, spineItems, tocLabels) {
    const navPoints = spineItems.map((f, i) =>
      `<navPoint id="np${i + 1}" playOrder="${i + 1}">` +
        `<navLabel><text>${escapeXml(tocLabels[i])}</text></navLabel>` +
        `<content src="${f}"/>` +
      `</navPoint>`
    ).join('\n  ');
    return `<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="${escapeXml(id)}"/></head>
  <docTitle><text>${escapeXml(title)}</text></docTitle>
  <navMap>
  ${navPoints}
  </navMap>
</ncx>`;
  }

  function makeNav(title, spineItems, tocLabels, lang) {
    const items = spineItems.map((f, i) =>
      `<li><a href="${f}">${escapeXml(tocLabels[i])}</a></li>`
    ).join('\n      ');
    return `<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="${escapeXml(lang)}" lang="${escapeXml(lang)}">
<head><meta charset="utf-8"/><title>Table of Contents</title></head>
<body>
  <nav epub:type="toc">
    <h1>Table of Contents</h1>
    <ol>
      ${items}
    </ol>
  </nav>
</body>
</html>`;
  }

  // ── Helpers ───────────────────────────────
  function looksLikePdf(file) {
    // Accept standard and legacy MIME types; fall back to extension check
    // because some browsers/OS report an empty MIME type for PDFs.
    return file.type === 'application/pdf' ||
           file.type === 'application/x-pdf' ||
           /\.pdf$/i.test(file.name);
  }

  function escapeXml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&apos;');
  }

  function sanitizeFilename(name) {
    return name.replace(/[^a-z0-9_\-. ]/gi, '_').trim() || 'book';
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function debugLog(msg) {
    const time = new Date().toLocaleTimeString();
    debugLines.push(`[${time}] ${msg}`);
    debugLogEl.value = debugLines.join('\n') + '\n';
    debugLogEl.scrollTop = debugLogEl.scrollHeight;
    // Auto-open the details panel when something is logged
    const details = debugLogEl.closest('details');
    if (details) details.open = true;
  }

  function clearDebugLog() {
    debugLines = [];
    debugLogEl.value = '';
  }

  function showProgress(pct, label) {
    progressSection.hidden = false;
    progressFill.style.width = pct + '%';
    progressLabel.textContent = label;
  }

  function showResult() {
    progressSection.hidden = true;
    resultSection.hidden   = false;
    convertBtn.disabled    = false;
  }

  function showError(msg) {
    progressSection.hidden = true;
    errorSection.hidden    = false;
    // Prefix with a decorative icon hidden from assistive technology
    errorMsg.innerHTML = '<span aria-hidden="true">⚠️ </span>' + escapeXml(msg);
    convertBtn.disabled    = false;
  }

  function hideResults() {
    resultSection.hidden = true;
    errorSection.hidden  = true;
    progressSection.hidden = true;
  }

})();
