"""Keyless dashboard access is restricted to a local browser request."""
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def _docker_default_gateway():
    """Return Docker's gateway only when executing inside a real container."""
    if not Path('/.dockerenv').is_file():
        return None
    try:
        for line in Path('/proc/net/route').read_text(encoding='ascii').splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 4 and fields[1] == '00000000' and int(fields[3], 16) & 0x2:
                raw = bytes.fromhex(fields[2])
                if len(raw) == 4:
                    return ip_address(raw[::-1])
    except (OSError, ValueError):
        pass
    return None


def _is_local_client(value):
    client = ip_address(value)
    return client.is_loopback or client == _docker_default_gateway()


def allow_local_admin(scope, settings):
    if not settings.rag_local_admin_enabled:
        return False
    try:
        if not _is_local_client(scope['client'][0]):
            return False
        headers = dict(scope['headers'])
        if any(key == b'forwarded' or key.startswith(b'x-forwarded-') for key in headers):
            return False
        host = headers.get(b'host', b'').decode('ascii')
        url = urlsplit(f'{scope["scheme"]}://{host}')
        if url.hostname not in {'localhost', '127.0.0.1', '::1'} or url.username or url.password:
            return False
        if url.path or url.query or url.fragment:
            return False
        port = url.port  # Validate a supplied port even when Origin is absent.
        if headers.get(b'x-rag-local-ui') != b'1':
            return False
        if headers.get(b'sec-fetch-site', b'same-origin') not in {b'same-origin', b'none'}:
            return False
        origin = headers.get(b'origin')
        if origin:
            source = urlsplit(origin.decode('ascii'))
            default_port = 443 if url.scheme == 'https' else 80
            if ((source.scheme, source.hostname, source.port or default_port) !=
                    (url.scheme, url.hostname, port or default_port)):
                return False
        return True
    except (ValueError, KeyError, TypeError, UnicodeError):
        return False
