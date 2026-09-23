"""Opening-handshake behavior (RFC 6455 section 4)."""

import socket
import threading
import unittest

from wsmini.exceptions import HandshakeError
from wsmini.handshake import (
    compute_accept, server_handshake, client_handshake, new_client_key,
)
from wsmini.connection import SocketReader


def run_server_handshake(server_sock, request_bytes, **kwargs):
    """Feed a raw request to server_handshake; return (error, response)."""
    client_sock = server_sock  # placeholder, replaced below
    a, b = socket.socketpair()
    b.sendall(request_bytes)
    error = None
    try:
        server_handshake(SocketReader(a), a, **kwargs)
    except HandshakeError as exc:
        error = exc
    b.settimeout(1.0)
    response = b""
    try:
        while b"\r\n\r\n" not in response:
            chunk = b.recv(4096)
            if not chunk:
                break
            response += chunk
    except socket.timeout:
        pass
    a.close()
    b.close()
    return error, response


def valid_request(**overrides):
    lines = [
        "GET /chat HTTP/1.1",
        "Host: example.test",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % new_client_key(),
        "Sec-WebSocket-Version: 13",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class AcceptTokenTest(unittest.TestCase):
    def test_rfc6455_example(self):
        # The worked example from RFC 6455 section 1.3.
        self.assertEqual(
            compute_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


class ServerHandshakeTest(unittest.TestCase):
    def test_valid_request_gets_101(self):
        error, response = run_server_handshake(None, valid_request())
        self.assertIsNone(error)
        self.assertIn(b"HTTP/1.1 101 Switching Protocols\r\n", response)
        self.assertIn(b"Upgrade: websocket\r\n", response)
        self.assertIn(b"Sec-WebSocket-Accept: ", response)

    def test_non_get_rejected_405(self):
        request = valid_request().replace(b"GET ", b"POST ", 1)
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"405 Method Not Allowed", response)

    def test_http_10_rejected(self):
        request = valid_request().replace(b"HTTP/1.1", b"HTTP/1.0")
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"400 Bad Request", response)

    def test_missing_upgrade_rejected(self):
        request = valid_request().replace(b"Upgrade: websocket\r\n", b"")
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"400 Bad Request", response)

    def test_missing_connection_upgrade_rejected(self):
        request = valid_request().replace(b"Connection: Upgrade\r\n", b"")
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"400 Bad Request", response)

    def test_missing_key_rejected(self):
        request = b"\r\n".join(
            line for line in valid_request().split(b"\r\n")
            if not line.startswith(b"Sec-WebSocket-Key"))
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"400 Bad Request", response)

    def test_non_base64_key_rejected(self):
        request = valid_request()
        key = request.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
        request = request.replace(key, b"!!!not-base64!!!")
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"400 Bad Request", response)

    def test_short_key_rejected(self):
        import base64
        request = valid_request()
        key = request.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
        request = request.replace(key, base64.b64encode(b"too-short"))
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"400 Bad Request", response)

    def test_wrong_version_gets_426(self):
        request = valid_request().replace(
            b"Sec-WebSocket-Version: 13", b"Sec-WebSocket-Version: 8")
        error, response = run_server_handshake(None, request)
        self.assertIsInstance(error, HandshakeError)
        self.assertIn(b"426 Upgrade Required", response)
        self.assertIn(b"Sec-WebSocket-Version: 13", response)

    def test_subprotocol_negotiated(self):
        a, b = socket.socketpair()
        request = valid_request().replace(
            b"\r\n\r\n",
            b"\r\nSec-WebSocket-Protocol: chat, super\r\n\r\n")
        b.sendall(request)
        chosen = server_handshake(SocketReader(a), a,
                                  subprotocols=["super", "other"])
        self.assertEqual(chosen, "super")
        a.close()
        b.close()


class ClientHandshakeTest(unittest.TestCase):
    def _run_client(self, server_response, url="ws://example.test/chat"):
        a, b = socket.socketpair()
        result = {}

        def do_client():
            try:
                result["value"] = client_handshake(a, url)
            except HandshakeError as exc:
                result["error"] = exc

        thread = threading.Thread(target=do_client)
        thread.start()
        request = b.recv(4096)  # the client's opening request
        b.sendall(server_response)
        thread.join(timeout=2.0)
        a.close()
        b.close()
        return request, result

    def test_valid_response_accepted(self):
        request_holder = []

        def responder():
            pass  # response computed from request below

        # We need the key from the request, so do the exchange manually.
        a, b = socket.socketpair()
        result = {}

        def do_client():
            result["value"] = client_handshake(a, "ws://example.test/chat")

        thread = threading.Thread(target=do_client)
        thread.start()
        request = b.recv(4096)
        key = [line.split(b": ", 1)[1] for line in request.split(b"\r\n")
               if line.startswith(b"Sec-WebSocket-Key")][0].decode()
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % compute_accept(key)
        ).encode("ascii")
        b.sendall(response)
        thread.join(timeout=2.0)
        a.close()
        b.close()
        self.assertIn("value", result)
        self.assertIn(b"GET /chat HTTP/1.1", request)
        self.assertIn(b"Upgrade: websocket", request)

    def test_bad_accept_token_rejected(self):
        _, result = self._run_client(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: bogus\r\n\r\n")
        self.assertIsInstance(result.get("error"), HandshakeError)

    def test_non_101_rejected(self):
        _, result = self._run_client(
            b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        self.assertIsInstance(result.get("error"), HandshakeError)


if __name__ == "__main__":
    unittest.main()
