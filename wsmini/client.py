"""WebSocket client entry point."""

import socket
from urllib.parse import urlsplit

from .connection import WSConnection, SocketReader, STATE_OPEN
from .exceptions import HandshakeError
from .handshake import client_handshake


def connect(url, subprotocols=None, extra_headers=None, timeout=None,
            **conn_options):
    """Connect to a ``ws://`` URL and return an OPEN WSConnection.

    ``conn_options`` are forwarded to WSConnection (max_message_size,
    ping_interval, ...). Raises HandshakeError on a failed upgrade and
    OSError on TCP-level failures. ``wss://`` (TLS) is not supported.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "ws":
        raise HandshakeError(
            "only ws:// URLs are supported, got %r" % parsed.scheme)
    host = parsed.hostname
    port = parsed.port or 80
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        reader = SocketReader(sock)
        chosen, _, _ = client_handshake(
            sock, url, subprotocols=subprotocols,
            extra_headers=extra_headers, reader=reader)
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return WSConnection(sock, reader=reader, is_client=True,
                        state=STATE_OPEN, subprotocol=chosen,
                        **conn_options)
