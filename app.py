#!/usr/bin/env python3
"""Small API wrapper around pdf_to_epub.py and format_epub.py.

This service keeps the browser UI simple while reusing the higher-quality Python
conversion pipeline. The frontend uploads a PDF, polls job status, and downloads
the resulting EPUB when the server-side job completes.

Run locally with:
    python -m uvicorn app:app --host 127.0.0.1 --port 8000

Render deployment:
    Uses render.yaml + Dockerfile so Tesseract is installed in the container.
"""

from __future__ import annotations

import json
import os
import queue as _queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

REPO_ROOT = Path(__file__).resolve().parent
TEMP_ROOT = Path(tempfile.gettempdir()) / 'pdf-to-epub-jobs'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv('PDF_TO_EPUB_MAX_UPLOAD_MB', '50'))
JOB_TTL_SECONDS = int(os.getenv('PDF_TO_EPUB_JOB_TTL_SECONDS', '3600'))
MAX_LOG_LINES = 200
# Maximum render scale accepted from API callers.  Scale 2.0 (144 effective DPI)
# is sufficient for high-quality Tesseract OCR.  Higher values grow the per-page
# pixmap quadratically and exhaust the service's 512 MB RAM budget on Render Starter.
MAX_SCALE = float(os.getenv('PDF_TO_EPUB_MAX_SCALE', '2.0'))

app = FastAPI(title='PDF to EPUB API')

cors_origins = [
    origin.strip()
    for origin in os.getenv('PDF_TO_EPUB_CORS_ORIGINS', '').split(',')
    if origin.strip()
]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=['*'],
        allow_headers=['*'],
    )

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# ── Disk persistence ──────────────────────────────────────────────────────────
# Job metadata is persisted to a small JSON file inside each job's work
# directory.  This lets the API recover gracefully when the server restarts
# mid-conversion: a poll request that misses the in-memory dict can still
# load the last-known state from disk and report a clear error to the browser
# instead of returning a confusing 404 "Job not found".

_PERSIST_FIELDS = (
    'id', 'status', 'progress', 'message',
    'created_at', 'updated_at', 'finished_at',
    'work_dir', 'input_path', 'output_path', 'output_name',
    'source_name', 'title', 'lang', 'scale', 'no_images',
)

# UUID hex strings produced by uuid.uuid4().hex are always 32 lower-case hex
# characters.  Validating before path construction prevents path-traversal
# attacks from malicious job_id values supplied via the URL.
_JOB_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def _job_state_file(work_dir: str | Path) -> Path:
    return Path(work_dir) / 'job.json'


def _save_job_to_disk(job: dict) -> None:
    """Persist the public fields of *job* to its work directory.

    Called without the jobs_lock held only when *job* is already owned by the
    current execution context (i.e. it was just created or a status update is
    being written).  Disk write failures are silently swallowed so they never
    crash the worker thread.
    """
    work_dir = job.get('work_dir')
    if not work_dir:
        return
    try:
        # Guard against path traversal: work_dir must be contained within TEMP_ROOT.
        resolved = Path(work_dir).resolve()
        if not resolved.is_relative_to(TEMP_ROOT.resolve()):
            return
        payload = {k: job.get(k) for k in _PERSIST_FIELDS if k in job}
        _job_state_file(resolved).write_text(
            json.dumps(payload, indent=2), encoding='utf-8'
        )
    except Exception:
        pass


def _load_job_from_disk(job_id: str) -> dict | None:
    """Try to restore job state for *job_id* from disk.

    Returns the job dict on success, or None if the state file does not exist
    or is unreadable.  Jobs found in a non-terminal state are marked as failed
    because a server restart means their subprocess is no longer running.
    """
    # Reject any job_id that is not a plain UUID hex string to prevent
    # path-traversal attacks (e.g. job_id = "../../etc/passwd").
    if not _JOB_ID_RE.match(job_id):
        return None
    state_file = TEMP_ROOT / job_id / 'job.json'
    # Defense-in-depth: verify the constructed path is inside TEMP_ROOT.
    if not state_file.resolve().is_relative_to(TEMP_ROOT.resolve()):
        return None
    if not state_file.exists():
        return None
    try:
        payload: dict = json.loads(state_file.read_text(encoding='utf-8'))
    except Exception:
        return None
    if payload.get('id') != job_id:
        return None
    # Subprocess is dead after restart -- mark in-flight jobs as failed.
    if payload.get('status') in ('queued', 'running'):
        now = time.time()
        payload['status'] = 'failed'
        payload['message'] = (
            'Conversion was interrupted because the server restarted. '
            'Please upload the file again.'
        )
        payload['finished_at'] = now
        payload['updated_at'] = now
    payload.setdefault('logs', [])
    return payload


# ── Job queue ─────────────────────────────────────────────────────────────────
# A single-worker FIFO queue ensures only one conversion runs at a time, keeping
# peak RAM use within the Render Starter 512 MB limit.  Concurrent requests are
# accepted immediately and held as 'queued' until the worker is free.

_job_queue: _queue.Queue[str] = _queue.Queue()


def _queue_worker() -> None:
    """Single background thread — drains _job_queue one job at a time."""
    while True:
        job_id = _job_queue.get()
        try:
            _run_job(job_id)
        except Exception as exc:
            # Catch-all so the worker never dies.  _run_job already updates the
            # job to 'failed'; this branch fires only for unexpected errors.
            try:
                _update_job(
                    job_id,
                    status='failed',
                    message=f'Unexpected error: {exc}',
                    finished_at=time.time(),
                )
                _append_log(job_id, f'[worker] unexpected error: {exc}')
            except Exception:
                pass  # job may already be cleaned up
        finally:
            _job_queue.task_done()


_worker_thread = threading.Thread(target=_queue_worker, daemon=True, name='epub-worker')
_worker_thread.start()


def _get_queue_position(job_id: str) -> int | None:
    """Return the 1-based queue position for *job_id*, or None if not in queue.

    This snapshot is taken under the queue's internal mutex so the list is
    consistent at the moment of the call.  A small race exists between the
    snapshot and the caller using the result: the worker may dequeue the item
    immediately after the mutex is released, causing the position to drop to
    None even though the job is still in its 'queued' state for a brief moment.
    This is harmless — the next poll will reflect the updated status.
    """
    with _job_queue.mutex:
        queue_list = list(_job_queue.queue)
    try:
        return queue_list.index(job_id) + 1
    except ValueError:
        return None


def _cleanup_expired_jobs() -> None:
    now = time.time()
    expired_ids: list[str] = []
    with jobs_lock:
        for job_id, job in jobs.items():
            if job['status'] not in ('completed', 'failed'):
                continue
            finished_at = job.get('finished_at') or job['updated_at']
            if now - finished_at > JOB_TTL_SECONDS:
                expired_ids.append(job_id)
        for job_id in expired_ids:
            work_dir = jobs[job_id].get('work_dir')
            if work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)
            del jobs[job_id]


def _sanitize_stem(name: str) -> str:
    stem = re.sub(r'[^A-Za-z0-9._ -]+', '_', name).strip(' ._')
    return stem or 'book'


def _get_job(job_id: str) -> dict:
    _cleanup_expired_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            return dict(job)
    # Not in memory — try to restore from disk (e.g. after a server restart).
    restored = _load_job_from_disk(job_id)
    if restored is None:
        raise HTTPException(status_code=404, detail='Job not found')
    # Cache the restored entry so subsequent polls are fast.
    with jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = restored
    return dict(restored)


def _update_job(job_id: str, **fields) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.update(fields)
        job['updated_at'] = time.time()
        # Persist to disk whenever the job reaches a terminal state so the
        # result survives a server restart (e.g. the download URL stays valid).
        if fields.get('status') in ('completed', 'failed'):
            _save_job_to_disk(job)


def _append_log(job_id: str, line: str) -> None:
    clean = line.strip()
    if not clean:
        return
    with jobs_lock:
        job = jobs[job_id]
        logs = job.setdefault('logs', [])
        logs.append(clean)
        if len(logs) > MAX_LOG_LINES:
            del logs[:-MAX_LOG_LINES]
        job['updated_at'] = time.time()


def _parse_progress(line: str, current_progress: int) -> tuple[int, str | None]:
    match = re.search(r'(?:OCR page|Page)\s+(\d+)/(\d+)', line)
    if match:
        current = int(match.group(1))
        total = max(1, int(match.group(2)))
        progress = 10 + int((current / total) * 75)
        # Use max() so the bar never goes backwards (page messages are now
        # emitted during EPUB assembly in the streaming approach).
        return max(current_progress, min(progress, 89)), f'Processing page {current} of {total}...'
    if 'Loading PDF' in line:
        return max(current_progress, 5), 'Loading PDF...'
    if 'Pages:' in line:
        return max(current_progress, 10), 'Preparing conversion...'
    if 'Building EPUB' in line:
        return max(current_progress, 90), 'Building EPUB...'
    if 'Formatting EPUB' in line:
        return max(current_progress, 95), 'Formatting EPUB...'
    if 'Done! Saved to:' in line or 'Formatted! Saved to:' in line:
        return 100, 'Download ready.'
    return current_progress, None


def _stream_process_output(job_id: str, process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    buffer = ''
    while True:
        chunk = process.stdout.read(1)
        if chunk == '' and process.poll() is not None:
            break
        if not chunk:
            continue
        if chunk in '\r\n':
            if buffer.strip():
                _append_log(job_id, buffer)
                job = _get_job(job_id)
                progress, message = _parse_progress(buffer, job.get('progress', 0))
                update_fields = {'progress': progress}
                if message:
                    update_fields['message'] = message
                _update_job(job_id, **update_fields)
            buffer = ''
            continue
        buffer += chunk
    if buffer.strip():
        _append_log(job_id, buffer)


def _run_job(job_id: str) -> None:
    job = _get_job(job_id)
    input_path = Path(job['input_path'])
    output_path = Path(job['output_path'])

    cmd = [
        sys.executable,
        str(REPO_ROOT / 'pdf_to_epub.py'),
        str(input_path),
        '--output',
        str(output_path),
        '--format',
    ]
    if job.get('title'):
        cmd.extend(['--title', job['title']])
    if job.get('lang'):
        cmd.extend(['--lang', job['lang']])
    if job.get('scale'):
        cmd.extend(['--scale', str(job['scale'])])
    if job.get('no_images'):
        cmd.append('--no-images')

    _update_job(job_id, status='running', progress=3, message='Starting conversion...')
    _append_log(job_id, 'Running: ' + ' '.join(cmd))

    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=0,
        )
    except Exception as exc:
        _update_job(
            job_id,
            status='failed',
            message=f'Failed to start conversion: {exc}',
            finished_at=time.time(),
        )
        return

    _stream_process_output(job_id, process)
    return_code = process.wait()

    if return_code == 0 and output_path.exists():
        _update_job(
            job_id,
            status='completed',
            progress=100,
            message='Download ready.',
            finished_at=time.time(),
        )
        return

    _update_job(
        job_id,
        status='failed',
        message='Conversion failed. See logs for details.',
        finished_at=time.time(),
    )


@app.get('/api/health')
def health() -> dict:
    return {'status': 'ok'}


@app.post('/api/convert')
async def create_conversion_job(
    pdf: UploadFile = File(...),
    title: str = Form(''),
    lang: str = Form('eng'),
    scale: float = Form(1.5),
    no_images: bool = Form(False),
) -> JSONResponse:
    _cleanup_expired_jobs()

    # Cap render scale to keep memory use within the Render Starter limit (512 MB).
    # At 2.0x a single A4 page occupies ~26 MB uncompressed; beyond that OOM risk
    # grows rapidly.  1.5x is the sweet-spot for OCR quality vs. memory.
    scale = min(scale, 2.0)

    filename = pdf.filename or 'upload.pdf'
    if not filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='Only PDF uploads are supported.')

    # Clamp scale to avoid runaway memory on the Render Starter instance.
    scale = min(scale, MAX_SCALE)

    job_id = uuid.uuid4().hex
    work_dir = TEMP_ROOT / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    input_name = _sanitize_stem(Path(filename).stem) + '.pdf'
    output_name = _sanitize_stem(Path(filename).stem) + '.epub'
    input_path = work_dir / input_name
    output_path = work_dir / output_name

    size_limit = MAX_UPLOAD_MB * 1024 * 1024
    total_bytes = 0
    with input_path.open('wb') as handle:
        while True:
            chunk = await pdf.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > size_limit:
                shutil.rmtree(work_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=413,
                    detail=f'Upload exceeds the {MAX_UPLOAD_MB} MB limit.',
                )
            handle.write(chunk)
    await pdf.close()

    job = {
        'id': job_id,
        'status': 'queued',
        'progress': 0,
        'message': 'Queued for conversion.',
        'created_at': time.time(),
        'updated_at': time.time(),
        'work_dir': str(work_dir),
        'input_path': str(input_path),
        'output_path': str(output_path),
        'output_name': output_name,
        'source_name': filename,
        'title': title.strip(),
        'lang': lang,
        'scale': scale,
        'no_images': no_images,
        'logs': [],
    }
    with jobs_lock:
        jobs[job_id] = job
    # Persist immediately so a server restart before the job completes can
    # still report a meaningful status to the browser instead of a bare 404.
    _save_job_to_disk(job)

    _job_queue.put(job_id)

    return JSONResponse(
        {
            'job_id': job_id,
            'status_url': f'/api/jobs/{job_id}',
            'download_url': f'/api/jobs/{job_id}/download',
        },
        status_code=202,
    )


@app.get('/api/jobs/{job_id}')
def get_job_status(job_id: str) -> dict:
    job = _get_job(job_id)

    # Compute live queue position while the job is waiting.
    queue_position: int | None = None
    message = job['message']
    if job['status'] == 'queued':
        queue_position = _get_queue_position(job_id)
        if queue_position is not None:
            message = (
                f'Position {queue_position} in queue — '
                'conversion typically takes several minutes per PDF.'
            )

    response = {
        'id': job['id'],
        'status': job['status'],
        'progress': job['progress'],
        'message': message,
        'source_name': job['source_name'],
        'queue_position': queue_position,
        'logs': job.get('logs', []),
    }
    if job['status'] == 'completed':
        response['download_url'] = f'/api/jobs/{job_id}/download'
    return response


@app.get('/api/jobs/{job_id}/download')
def download_job_output(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    if job['status'] != 'completed':
        raise HTTPException(status_code=409, detail='Job is not complete yet.')
    output_path = Path(job['output_path'])
    if not output_path.exists():
        raise HTTPException(status_code=404, detail='Output file is no longer available.')
    return FileResponse(
        path=output_path,
        media_type='application/epub+zip',
        filename=job['output_name'],
    )


@app.delete('/api/jobs/{job_id}')
def delete_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found')
    shutil.rmtree(job.get('work_dir', ''), ignore_errors=True)
    return {'deleted': True}
