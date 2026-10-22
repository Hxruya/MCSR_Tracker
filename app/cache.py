"""In-process cache for slow results that only change when an import adds data.

Entries expire after TTL seconds, and the importer calls clear() as soon as new matches are stored, so a fresh
import shows up immediately. Bypassed under tests, where every test has different data.
"""
import threading
import time

from flask import current_app, has_app_context

TTL = 600   # seconds
_entries = {}
_lock = threading.Lock()


def cached(key, compute, ttl=TTL):
    """compute() once per key per ttl seconds. An exception from compute() is not cached."""
    if has_app_context() and current_app.testing:
        return compute()
    with _lock:
        hit = _entries.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = compute()
    with _lock:
        _entries[key] = (time.monotonic(), value)
    return value


def clear():
    with _lock:
        _entries.clear()
