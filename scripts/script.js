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
      reader.onload  = (e) => resolve(e.target.result);
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

    // One XHTML chapter per page
    const spineItems = pageTexts.map((text, i) => {
      const fname = `page${i + 1}.xhtml`;
      oebps.file(fname, makeXhtml(title, i + 1, text, lang));
      return fname;
    });

    // content.opf  (package document)
    oebps.file('content.opf', makeOpf(id, title, lang, spineItems));

    // toc.ncx  (legacy nav for older readers)
    oebps.file('toc.ncx', makeNcx(id, title, spineItems));

    // nav.xhtml  (EPUB 3 nav document)
    oebps.file('nav.xhtml', makeNav(title, spineItems, lang));

    return zip.generateAsync({ type: 'blob', mimeType: 'application/epub+zip' });
  }

  function makeXhtml(title, pageNum, text, lang) {
    const escaped = escapeXml(text);
    // Preserve paragraph structure: blank lines become spacer paragraphs
    const body = escaped.split('\n').map(l => l.trim()
      ? `<p>${l}</p>`
      : `<p>&#160;</p>`
    ).join('\n');
    return `<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="${escapeXml(lang)}" lang="${escapeXml(lang)}">
<head><meta charset="utf-8"/><title>${escapeXml(title)} — Page ${pageNum}</title></head>
<body>
<h2>Page ${pageNum}</h2>
${body || '<p> </p>'}
</body>
</html>`;
  }

  function makeOpf(id, title, lang, spineItems) {
    const manifestItems = spineItems.map((f, i) =>
      `<item id="page${i + 1}" href="${f}" media-type="application/xhtml+xml"/>`
    ).join('\n    ');
    const navItem =
      '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>';
    const ncxItem =
      '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>';
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
    ${manifestItems}
  </manifest>
  <spine toc="ncx">
    ${spineRefs}
  </spine>
</package>`;
  }

  function makeNcx(id, title, spineItems) {
    const navPoints = spineItems.map((f, i) =>
      `<navPoint id="np${i + 1}" playOrder="${i + 1}">` +
        `<navLabel><text>Page ${i + 1}</text></navLabel>` +
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

  function makeNav(title, spineItems, lang) {
    const items = spineItems.map((f, i) =>
      `<li><a href="${f}">Page ${i + 1}</a></li>`
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
    debugLogEl.value += `[${time}] ${msg}\n`;
    debugLogEl.scrollTop = debugLogEl.scrollHeight;
    // Auto-open the details panel when something is logged
    const details = debugLogEl.closest('details');
    if (details) details.open = true;
  }

  function clearDebugLog() {
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
