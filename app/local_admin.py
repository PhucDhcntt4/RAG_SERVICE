"""Keyless dashboard access is restricted to a direct loopback browser request."""
from ipaddress import ip_address
from urllib.parse import urlsplit


def allow_local_admin(scope, settings):
    if not settings.rag_local_admin_enabled:
        return False
    try:
        if not ip_address(scope['client'][0]).is_loopback:
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
