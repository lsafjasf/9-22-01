"""Exception hierarchy for wsmini."""


class WebSocketError(Exception):
    """Base class for every wsmini error."""


class HandshakeError(WebSocketError):
    """The HTTP upgrade handshake failed."""


class ProtocolError(WebSocketError):
    """A received frame violated RFC 6455.

    The offending connection is terminated with the close code carried in
    ``close_code`` (1002 for protocol errors, 1007/1009/1011 when a more
    specific reason applies).
    """

    def __init__(self, message, close_code=1002):
        super().__init__(message)
        self.close_code = close_code


class CloseError(WebSocketError):
    """The connection reached a terminal state (abnormal close or timeout).

    ``close_code`` is the effective code: the peer's code for a normal close,
    1006 for abnormal loss, 1011 for internal errors.
    """

    def __init__(self, message, close_code=1006, reason=""):
        super().__init__(message)
        self.close_code = close_code
        self.reason = reason
