"""Gunicorn entrypoint for the IFU compute back-end.

    gunicorn -c gunicorn.conf.py wsgi:app

boot() runs once per worker at import.  It's cheap now (no source
preload -- sources import lazily on first use) -- just data-dir setup
and the idempotent figure->view migration.
"""
from serve import app, boot

boot()
