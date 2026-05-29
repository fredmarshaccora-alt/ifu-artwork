"""Gunicorn config for the IFU compute back-end.

SINGLE worker on purpose: the in-memory shape cache (_SHAPES) and the
OCCT render lock (_HLR_LOCK) assume ONE process.  Multiple workers would
each import sources independently (duplicate RAM) and the lock wouldn't
serialise OCCT across them.  Threads let cheap requests (healthz,
figures CRUD, static) stay responsive while one HLR render holds the
lock.  timeout=0 disables the worker-kill so a long render of a big
assembly (can be minutes) is never aborted mid-flight.

Scale path when one box isn't enough: run N of these single-worker
instances behind a load balancer, each with its own _SHAPES; they share
the figures/views/etc. on the mounted disk (different files, so writes
don't collide in practice).
"""
import os

bind = "0.0.0.0:" + (os.environ.get("PORT") or "10000")
workers = 1
threads = int(os.environ.get("WEB_THREADS") or "8")
timeout = 0            # never kill a long OCCT render
graceful_timeout = 30
keepalive = 5
