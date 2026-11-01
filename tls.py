"""Verify HTTPS certificates against the operating system's trust store.

Python ships its own CA bundle (certifi). Networks that inspect HTTPS, such
as corporate proxies or antivirus web shields, re-sign traffic with a root
certificate that the OS trusts but certifi does not. truststore makes every
Python HTTPS client (httpx, requests, urllib3) use the OS store instead, so
certificate verification stays on everywhere.
"""

from __future__ import annotations

_done = False


def use_system_certificates() -> None:
    global _done
    if _done:
        return
    try:
        import truststore
    except ImportError:
        _done = True
        return
    truststore.inject_into_ssl()
    _done = True
