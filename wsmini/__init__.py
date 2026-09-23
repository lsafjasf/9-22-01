"""wsmini: a small, stdlib-only WebSocket (RFC 6455) client/server library.

Public surface (v1-compatible):
    WSConnection, serve, connect, WebSocketError
    send_message / recv_message style usage is via connection methods.
"""

from .exceptions import (
    WebSocketError,
    HandshakeError,
    ProtocolError,
    CloseError,
)
from .connection import (
    WSConnection,
    STATE_CONNECTING,
    STATE_OPEN,
    STATE_CLOSING,
    STATE_CLOSED,
)
from .server import serve
from .client import connect

__all__ = [
    "WSConnection",
    "serve",
    "connect",
    "WebSocketError",
    "HandshakeError",
    "ProtocolError",
    "CloseError",
    "STATE_CONNECTING",
    "STATE_OPEN",
    "STATE_CLOSING",
    "STATE_CLOSED",
]
