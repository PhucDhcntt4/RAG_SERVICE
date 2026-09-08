"""One-line DEBUG events correlated across HTTP, retrieval and generation."""
import json
import logging
from contextvars import ContextVar

request_id = ContextVar('rag_request_id', default='-')
logger = logging.getLogger('rag_service')


def debug_event(event, **fields):
    if not logger.isEnabledFor(logging.DEBUG):
        return
    # JSON quoting prevents query/metadata newlines from impersonating log entries.
    values = {'request_id': request_id.get(), **fields}
    logger.debug('%s %s', event, ' '.join(
        f'{key}={json.dumps(value, ensure_ascii=False, separators=(",", ":"))}'
        for key, value in values.items()))
