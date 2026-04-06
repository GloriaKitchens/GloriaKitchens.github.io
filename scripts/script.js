/* Browser upload client for the Python conversion API */

(function () {
  'use strict';

  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  const fileNameEl = document.getElementById('fileName');
  const langSelect = document.getElementById('langSelect');
  const bookTitleInput = document.getElementById('bookTitle');
  const convertBtn = document.getElementById('convertBtn');
  const progressSection = document.getElementById('progressSection');
  const progressFill = document.getElementById('progressFill');
  const progressLabel = document.getElementById('progressLabel');
  const resultSection = document.getElementById('resultSection');
  const downloadLink = document.getElementById('downloadLink');
  const errorSection = document.getElementById('errorSection');
  const errorMsg = document.getElementById('errorMsg');
  const debugLogEl = document.getElementById('debugLog');

  const API_BASE = (document.body && document.body.dataset.apiBase) || '';
  const NEEDS_EXPLICIT_API_BASE =
    /\.github\.io$/i.test(window.location.hostname) ||
    window.location.protocol === 'file:';

  let selectedFile = null;
  let currentJobId = null;
  let pollTimer = null;

  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file && !looksLikePdf(file)) {
      showError('Please select a valid PDF file.');
      fileInput.value = '';
      return;
    }
    handleFile(file);
  });

  dropZone.addEventListener('click', (event) => {
    if (event.target.closest('label')) return;
    fileInput.click();
  });

  dropZone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      fileInput.click();
    }
  });

  dropZone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropZone.classList.add('drag-over');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-over');
  });

  dropZone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropZone.classList.remove('drag-over');
    const file = event.dataTransfer.files[0];
    if (file && looksLikePdf(file)) {
      handleFile(file);
      return;
    }
    showError('Please drop a valid PDF file.');
  });

  convertBtn.addEventListener('click', async () => {
    if (!selectedFile) return;
    if (!backendConfigured()) {
      const message =
        'No Python API backend is configured for this site. On GitHub Pages, set body[data-api-base] to your deployed backend URL.';
      debugLog(message);
      showError(message);
      return;
    }

    stopPolling();
    currentJobId = null;
    hideResults();
    clearDebugLog();
    convertBtn.disabled = true;

    try {
      showProgress(5, 'Uploading PDF...');
      debugLog('Uploading PDF to the Python backend...');

      const formData = new FormData();
      formData.append('pdf', selectedFile);
      formData.append('title', bookTitleInput.value.trim());
      formData.append('lang', langSelect.value);
      formData.append('scale', '2.0');
      formData.append('no_images', 'false');

      const response = await fetch(apiUrl('/api/convert'), {
        method: 'POST',
        body: formData,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || 'Failed to start conversion.');
      }

      currentJobId = payload.job_id;
      debugLog(`Job queued: ${currentJobId}`);
      showProgress(8, 'Job queued...');
      startPolling(currentJobId);
    } catch (error) {
      showError('Could not start conversion: ' + (error.message || error));
      convertBtn.disabled = false;
    }
  });

  function startPolling(jobId) {
    pollJob(jobId);
    pollTimer = window.setInterval(() => pollJob(jobId), 1500);
  }

  async function pollJob(jobId) {
    try {
      const response = await fetch(apiUrl(`/api/jobs/${jobId}`), {
        cache: 'no-store',
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || 'Failed to fetch job status.');
      }

      showProgress(payload.progress || 0, payload.message || 'Processing...');
      syncLogs(payload.logs || []);

      if (payload.status === 'completed') {
        stopPolling();
        debugLog('Conversion complete. Preparing download...');
        downloadLink.href = apiUrl(payload.download_url || `/api/jobs/${jobId}/download`);
        downloadLink.download = selectedFile.name.replace(/\.pdf$/i, '') + '.epub';
        showResult();
        convertBtn.disabled = false;
        return;
      }

      if (payload.status === 'failed') {
        stopPolling();
        throw new Error(payload.message || 'Conversion failed.');
      }
    } catch (error) {
      stopPolling();
      showError('Conversion failed: ' + (error.message || error));
      convertBtn.disabled = false;
    }
  }

  function handleFile(file) {
    if (!file) return;
    selectedFile = file;
    fileNameEl.textContent = `${file.name} (${formatBytes(file.size)})`;
    convertBtn.disabled = false;
    hideResults();
    debugLog(`File selected: ${file.name}`);
  }

  function apiUrl(path) {
    if (!API_BASE) return path;
    return API_BASE.replace(/\/$/, '') + path;
  }

  function backendConfigured() {
    if (API_BASE) return true;
    return !NEEDS_EXPLICIT_API_BASE;
  }

  function syncLogs(lines) {
    debugLogEl.value = lines.join('\n');
    debugLogEl.scrollTop = debugLogEl.scrollHeight;
  }

  function debugLog(line) {
    const lines = debugLogEl.value ? debugLogEl.value.split('\n') : [];
    lines.push(line);
    debugLogEl.value = lines.join('\n');
    debugLogEl.scrollTop = debugLogEl.scrollHeight;
  }

  function clearDebugLog() {
    debugLogEl.value = '';
  }

  function stopPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function showProgress(percent, text) {
    progressSection.hidden = false;
    progressFill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    progressLabel.textContent = text;
    resultSection.hidden = true;
    errorSection.hidden = true;
  }

  function showResult() {
    progressSection.hidden = false;
    resultSection.hidden = false;
    errorSection.hidden = true;
  }

  function showError(message) {
    errorMsg.textContent = message;
    errorSection.hidden = false;
    resultSection.hidden = true;
  }

  function hideResults() {
    resultSection.hidden = true;
    errorSection.hidden = true;
  }

  function looksLikePdf(file) {
    return (
      file.type === 'application/pdf' ||
      /\.pdf$/i.test(file.name || '')
    );
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
})();
