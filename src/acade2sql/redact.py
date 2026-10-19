"""Hide secrets in connection URLs and in text derived from them (exception messages, log lines).

* :func:`mask_url` - display form of a SQLAlchemy URL: userinfo password, secret-looking query
  parameters and ``PWD=``/``PASSWORD=`` inside ``odbc_connect`` become ``***``.
* :func:`redact` - remove those secrets (and anything that looks like one) from arbitrary text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import URL, make_url

log = logging.getLogger(__name__)

MASK = "***"
_ENCODED_MASK = quote(MASK, safe="")  # "%2A%2A%2A": how render_as_string encodes "***"

SECRET_KEY = re.compile(r"(?i)(pass|pwd|secret|token|key)")
ODBC_CONNECT = "odbc_connect"
# PWD=/PASSWORD= in an ODBC connection string: a braced value ("}}" escapes "}") or up to ";".
_ODBC_PWD = re.compile(r"(?i)\b(PWD|PASSWORD)=(\{(?:[^}]|\}\})*\}|[^;]*)")
# Free-text fallbacks used by redact(): "scheme://user:secret@" and "PWD=secret" (stops at
# ';', whitespace or a quote so the rest of an exception message survives).
_TEXT_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^:/@\s]*):([^@\s/]+)@")
_TEXT_PWD = re.compile(r"(?i)\b(PWD|PASSWORD)=(\{(?:[^}]|\}\})*\}|[^;\s'\"]*)")
# The same inside a URL-encoded odbc_connect value: PWD%3D<value> up to %3B ("{...}" = %7B...%7D).
_TEXT_PWD_ENCODED = re.compile(
    r"(?i)(?:(?<=%3B)|(?<![A-Za-z0-9]))(PWD|PASSWORD)%3D(%7B(?:%7D%7D|(?!%7D)[^\s])*%7D|(?:(?!%3B)[^;&\s'\"])*)"
)


def _parse(url: str | URL) -> URL | None:
    if isinstance(url, URL):
        return url
    try:
        return make_url(url)
    except (sa_exc.ArgumentError, ValueError, TypeError):
        return None


def _mask_odbc(value: str) -> str:
    return _ODBC_PWD.sub(lambda m: f"{m.group(1)}={MASK}", value)


def _mask_query_value(key: str, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
    if SECRET_KEY.search(key):
        return MASK if isinstance(value, str) else tuple(MASK for _ in value)
    if key.lower() == ODBC_CONNECT:
        return _mask_odbc(value) if isinstance(value, str) else tuple(_mask_odbc(v) for v in value)
    return value


def mask_url(url: str | URL) -> str:
    """Display form of ``url`` with every secret replaced by ``***``.

    SQLite URLs show the plain (un-escaped) filesystem path. A string that is not a parseable URL
    is masked with the free-text rules of :func:`redact`.
    """
    parsed = _parse(url)
    if parsed is None:
        return redact(str(url))
    if parsed.get_backend_name() == "sqlite" and not parsed.password and not parsed.query:
        return f"{parsed.drivername}:///{parsed.database or ''}"
    query = {k: _mask_query_value(k, v) for k, v in parsed.query.items()}
    masked = parsed.set(query=query)
    text = masked.render_as_string(hide_password=True)
    return text.replace(_ENCODED_MASK, MASK)


def _odbc_secrets(value: str) -> Iterable[str]:
    for m in _ODBC_PWD.finditer(value):
        raw = m.group(2)
        yield raw
        if raw.startswith("{") and raw.endswith("}"):
            yield raw[1:-1].replace("}}", "}")


def _url_secrets(url: str | URL) -> set[str]:
    parsed = _parse(url)
    secrets: set[str] = set()
    if parsed is None:
        text = str(url)
        secrets.update(m.group(2) for m in _TEXT_USERINFO.finditer(text))
        for m in _TEXT_PWD.finditer(text):
            secrets.update(_odbc_secrets(m.group(0)))
    else:
        if parsed.password:
            secrets.add(str(parsed.password))
        for key, value in parsed.query.items():
            values = (value,) if isinstance(value, str) else tuple(value)
            for v in values:
                if SECRET_KEY.search(key):
                    secrets.add(v)
                elif key.lower() == ODBC_CONNECT:
                    secrets.update(_odbc_secrets(v))
    expanded: set[str] = set()
    for s in secrets:
        if s:
            expanded.update({s, quote(s, safe=""), quote_plus(s, safe="")})
    return expanded


def redact(text: str, url: str | URL | None = None) -> str:
    """Return ``text`` with the secrets of ``url`` (raw and URL-encoded) replaced by ``***``.

    Also masks, independently of ``url``, ``scheme://user:secret@`` userinfo and ``PWD=``/
    ``PASSWORD=`` values (plain or URL-encoded as ``PWD%3D...``) found in the text itself.
    """
    out = str(text)
    if url is not None:
        for secret in sorted(_url_secrets(url), key=len, reverse=True):
            out = out.replace(secret, MASK)
    out = _TEXT_USERINFO.sub(lambda m: f"{m.group(1)}:{MASK}@", out)
    out = _TEXT_PWD.sub(lambda m: f"{m.group(1)}={MASK}", out)
    out = _TEXT_PWD_ENCODED.sub(lambda m: f"{m.group(1)}%3D{MASK}", out)
    return out
