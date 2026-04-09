#!/usr/bin/env python3
"""Low-overhead process memory helpers for the API and converter."""

from __future__ import annotations

import os


def _bytes_to_mb(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024 * 1024), 1)


def _read_linux_proc_status(pid: int) -> dict[str, float | None]:
    status_path = f'/proc/{pid}/status'
    rss_kb: int | None = None
    peak_kb: int | None = None
    try:
        with open(status_path, encoding='utf-8', errors='replace') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line.startswith('VmRSS:'):
                    parts = line.split()
                    if len(parts) >= 2:
                        rss_kb = int(parts[1])
                elif line.startswith('VmHWM:'):
                    parts = line.split()
                    if len(parts) >= 2:
                        peak_kb = int(parts[1])
    except OSError:
        return {'rss_mb': None, 'peak_rss_mb': None}

    return {
        'rss_mb': _bytes_to_mb(rss_kb * 1024) if rss_kb is not None else None,
        'peak_rss_mb': _bytes_to_mb(peak_kb * 1024) if peak_kb is not None else None,
    }


def _read_windows_process_memory(pid: int) -> dict[str, float | None]:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return {'rss_mb': None, 'peak_rss_mb': None}

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ('cb', wintypes.DWORD),
            ('PageFaultCount', wintypes.DWORD),
            ('PeakWorkingSetSize', ctypes.c_size_t),
            ('WorkingSetSize', ctypes.c_size_t),
            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPagedPoolUsage', ctypes.c_size_t),
            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
            ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
            ('PagefileUsage', ctypes.c_size_t),
            ('PeakPagefileUsage', ctypes.c_size_t),
        ]

    process_query_information = 0x0400
    process_vm_read = 0x0010
    access = process_query_information | process_vm_read

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)

    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return {'rss_mb': None, 'peak_rss_mb': None}

    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    try:
        ok = psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            return {'rss_mb': None, 'peak_rss_mb': None}
        return {
            'rss_mb': _bytes_to_mb(counters.WorkingSetSize),
            'peak_rss_mb': _bytes_to_mb(counters.PeakWorkingSetSize),
        }
    finally:
        kernel32.CloseHandle(handle)


def _read_resource_peak() -> float | None:
    try:
        import resource
    except ImportError:
        return None

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if peak <= 0:
        return None

    if os.uname().sysname == 'Darwin':
        return _bytes_to_mb(peak)
    return _bytes_to_mb(peak * 1024)


def get_process_memory_snapshot(pid: int | None = None) -> dict[str, float | None]:
    """Return current and peak resident memory for *pid* in MB when available."""
    target_pid = os.getpid() if pid is None else int(pid)

    if os.name == 'nt':
        return _read_windows_process_memory(target_pid)

    snapshot = _read_linux_proc_status(target_pid)
    if snapshot['rss_mb'] is not None or snapshot['peak_rss_mb'] is not None:
        return snapshot

    if target_pid == os.getpid():
        return {'rss_mb': None, 'peak_rss_mb': _read_resource_peak()}

    return {'rss_mb': None, 'peak_rss_mb': None}
