"""``{% asset 'ide/app.js' %}`` -> ``/static/ide/app.js?v=<content hash>``.

The dev server (runserver/daphne) serves static files with only a
Last-Modified header, so browsers may heuristically reuse an old app.js after
an upgrade and run stale UI code against a newer API. Fingerprinting the URL
by content forces a fetch whenever the file changes.
"""
from __future__ import annotations

import hashlib
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()
_cache: dict[str, tuple[float, str]] = {}


def _fingerprint(path: str) -> str | None:
    found = finders.find(path)
    if not found:
        return None
    mtime = os.path.getmtime(found)
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(found, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()[:12]
    _cache[path] = (mtime, digest)
    return digest


@register.simple_tag
def asset(path: str) -> str:
    url = static(path)
    fp = _fingerprint(path)
    return f"{url}?v={fp}" if fp else url
