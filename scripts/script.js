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

  let selectedFile = null;

  // ── PDF.js worker ─────────────────────────
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  // ── File selection ────────────────────────
  fileInput.addEventListener('change', () => {
    handleFile(fileInput.files[0]);
  });

  dropZone.addEventListener('click', () => fileInput.click());

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
    if (f && f.type === 'application/pdf') {
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
  }

  // ── Convert ───────────────────────────────
  convertBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    hideResults();
    convertBtn.disabled = true;
    showProgress(0, 'Loading PDF…');

    try {
      const title = bookTitleInput.value.trim() || selectedFile.name.replace(/\.pdf$/i, '') || 'My Book';
      const lang  = langSelect.value;

      // 1. Load PDF
      const arrayBuffer = await selectedFile.arrayBuffer();
      const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise;
      const totalPages = pdf.numPages;

      // 2. OCR each page
      const worker = await Tesseract.createWorker(lang, 1, {
        logger: () => {} // silence verbose logs
      });

      const pageTexts = [];
      for (let i = 1; i <= totalPages; i++) {
        const pct = Math.round(((i - 1) / totalPages) * 85);
        showProgress(pct, `Recognising page ${i} of ${totalPages}…`);

        const canvas = await renderPageToCanvas(pdf, i);
        const { data: { text } } = await worker.recognize(canvas);
        pageTexts.push(text.trim());
      }

      await worker.terminate();

      // 3. Build EPUB
      showProgress(90, 'Building EPUB…');
      const epubBlob = buildEpub(title, pageTexts);

      // 4. Offer download
      showProgress(100, 'Done!');
      const url = URL.createObjectURL(epubBlob);
      downloadLink.href = url;
      downloadLink.download = sanitizeFilename(title) + '.epub';
      showResult();
    } catch (err) {
      showError('Something went wrong: ' + (err.message || err));
      convertBtn.disabled = false;
    }
  });

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
  function buildEpub(title, pageTexts) {
    const zip  = new JSZip();
    const id   = 'book-' + Date.now();
    const lang = langSelect.value.split('_')[0]; // e.g. "chi_sim" → "chi"

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
      oebps.file(fname, makeXhtml(title, i + 1, text));
      return fname;
    });

    // content.opf  (package document)
    oebps.file('content.opf', makeOpf(id, title, lang, spineItems));

    // toc.ncx  (legacy nav for older readers)
    oebps.file('toc.ncx', makeNcx(id, title, spineItems));

    // nav.xhtml  (EPUB 3 nav document)
    oebps.file('nav.xhtml', makeNav(title, spineItems));

    return zip.generateAsync({ type: 'blob', mimeType: 'application/epub+zip' });
  }

  function makeXhtml(title, pageNum, text) {
    const escaped = escapeXml(text);
    // Preserve line breaks
    const body = escaped.split('\n').filter(l => l.trim()).map(l => `<p>${l}</p>`).join('\n');
    return `<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
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

  function makeNav(title, spineItems) {
    const items = spineItems.map((f, i) =>
      `<li><a href="${f}">Page ${i + 1}</a></li>`
    ).join('\n      ');
    return `<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">
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
    errorMsg.textContent   = msg;
    convertBtn.disabled    = false;
  }

  function hideResults() {
    resultSection.hidden = true;
    errorSection.hidden  = true;
    progressSection.hidden = true;
  }

})();
