"""Offline, standard-library privacy boundary for JSON-like task state.

Recognizable credentials are removed from narrative content. Identity and
resource metadata must use check_metadata instead, because replacing identity
text can alias distinct actors or resources. This is a conservative pattern
boundary, not a guarantee that arbitrary prose contains no private information.
"""

import json
import os
import re
import subprocess


class PrivacyError(Exception):
    """Privacy validation failed; messages deliberately contain no input text."""


REDACTED = '[REDACTED]'
_SENSITIVE_KEY = re.compile(
    r'^(?:password|passwd|pwd|secret|token|access_token|refresh_token|'
    r'api[_-]?key|client_secret|private[_-]?key|authorization|aws_secret_access_key)$',
    re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r'^(?:\[REDACTED\]|<[^<>\r\n]+>|\$\{[A-Za-z_][A-Za-z0-9_]*\}|'
    r'\*{3,}|REDACTED|YOUR[_ -][A-Za-z_ -]+|CHANGE[_ -]?ME|example|placeholder)$',
    re.IGNORECASE)
_PEM = re.compile(
    r'-----BEGIN ((?:[A-Z0-9 -]* )?PRIVATE KEY)-----.*?(?:-----END \1-----|\Z)',
    re.DOTALL)
_TOKENS = [re.compile(pattern) for pattern in (
    r'(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])',
    r'(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])',
    r'(?<![A-Za-z0-9_])sk-(?:(?:proj|svcacct|org)-)?[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_])',
    r'(?<![A-Za-z0-9_])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_])',
    r'(?<![A-Za-z0-9_])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9_])',
    r'(?<![A-Za-z0-9_])(?:xox[baprs]|xapp)-[A-Za-z0-9-]{8,}(?![A-Za-z0-9_])',
    r'(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_])',
)]
_BEARER = re.compile(r'\bBearer[ \t]+(\[REDACTED\]|[^\s\"\'<>;,}\]]+)', re.IGNORECASE)
_AUTH = re.compile(r'(\bAuthorization\s*[:=]\s*)(?:Bearer|Basic)\s+[^\s\"\'<>;,}\]]+', re.IGNORECASE)
_BEARER_DISCUSSION = frozenset(('authentication', 'authorization', 'token', 'tokens', 'scheme', 'header'))
_URL_AUTH = re.compile(r'(\b[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+(?::[^\s/@]*)?@')
# Quoted JSON keys and configuration assignments both carry credential context.
_ASSIGNMENT = re.compile(
    r'''(?P<prefix>(?<![\w-])(?:["']?)(?:password|passwd|pwd|secret|token|access_token|refresh_token|api[_-]?key|client_secret|private[_-]?key|authorization|aws_secret_access_key)(?:["']?)\s*[:=]\s*)(?P<value>\[REDACTED\]|\$\{[A-Za-z_][A-Za-z0-9_]*\}|<[^<>\r\n]+>|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;\}\]]+)''',
    re.IGNORECASE)


def _placeholder(value):
    return not value.strip() or bool(_PLACEHOLDER.fullmatch(value.strip()))


def _text(value):
    value = _PEM.sub(REDACTED, value)
    for pattern in _TOKENS:
        value = pattern.sub(REDACTED, value)
    value = _AUTH.sub(lambda m: m[1] + REDACTED, value)
    value = _BEARER.sub(lambda m: 'Bearer ' + (m[1] if _placeholder(m[1]) or m[1].lower() in _BEARER_DISCUSSION else REDACTED), value)
    value = _URL_AUTH.sub(lambda m: m[1] + REDACTED + '@', value)

    def assignment(match):
        raw = match['value']
        quote = raw[0] if raw[:1] in ('"', "'") else ''
        content = raw[1:-1] if quote else raw
        if _placeholder(content):
            return match[0]
        return match['prefix'] + quote + REDACTED + quote

    return _ASSIGNMENT.sub(assignment, value)


def _scrub(value, active):
    if len(active) > 100:
        raise PrivacyError("Privacy input exceeds nesting limit.")
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, (dict, list, tuple)):
        raise PrivacyError('Unsupported privacy input type.')
    identity = id(value)
    if identity in active:
        raise PrivacyError('Cyclic privacy input is not supported.')
    active.add(identity)
    try:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise PrivacyError('Privacy object keys must be strings.')
                clean_key = _text(key)
                if clean_key in result:
                    raise PrivacyError('Privacy redaction would collide object keys.')
                clean_item = _scrub(item, active)
                sensitive = _SENSITIVE_KEY.fullmatch(key) and item is not None and (
                    not isinstance(item, str) or not _placeholder(item))
                if sensitive:
                    result[clean_key] = REDACTED
                else:
                    result[clean_key] = clean_item
            return result
        items = [_scrub(item, active) for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    finally:
        active.remove(identity)


def scrub(value):
    """Return a recursively sanitized copy; never mutate the source value."""
    return _scrub(value, set())


def check_metadata(value):
    """Reject credential-bearing identity/resource data, returning safe input."""
    if scrub(value) != value:
        raise PrivacyError('Credential-shaped metadata is not allowed.')
    return value


def guard(value, scanner=None):
    """Sanitize, then optionally run an offline scanner over JSON on stdin.

    The scanner is an argument array, never a shell command. Exit 0 allows the
    sanitized object; exit 1 rejects a finding; all other failures reject too.
    Output and original subprocess exceptions are never surfaced to callers.
    Callers configure a BetterLeaks stdin/JSON adapter as appropriate for the
    installed CLI version. No network or external scanner runs by default.
    """
    clean = scrub(value)
    if scanner is None:
        return clean
    if not isinstance(scanner, (list, tuple)) or not scanner or any(
            not isinstance(arg, str) or not arg or '\x00' in arg for arg in scanner):
        raise PrivacyError('Privacy scanner requires an argument array.')
    try:
        payload = json.dumps(clean, ensure_ascii=False, allow_nan=False)
        result = subprocess.run(list(scanner), input=payload, text=True, encoding='utf-8',
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30, check=False,
                                env={'PATH': os.defpath, 'HOME': '/var/empty',
                                     'BETTERLEAKS_VALIDATE': 'false', 'BETTERLEAKS_ANALYZE': 'false'})
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise PrivacyError('Privacy scanner could not validate the content.') from None
    if result.returncode == 1:
        raise PrivacyError('Privacy scanner rejected the content.')
    if result.returncode != 0:
        raise PrivacyError('Privacy scanner could not validate the content.')
    return clean
