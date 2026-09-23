"""End-to-end tests: serve()/connect() over real TCP sockets."""

import threading
import unittest

from wsmini import (
    serve, connect, WebSocketError, HandshakeError,
    STATE_OPEN, STATE_CLOSED,
)


class EchoServerTest(unittest.TestCase):
    def setUp(self):
        self.server = serve("127.0.0.1", 0, self._echo)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.address[1]

    @staticmethod
    def _echo(conn):
        while True:
            message = conn.recv_message()
            if message is None:
                break
            conn.send_message(message)

    def test_echo_roundtrip(self):
        conn = connect("ws://127.0.0.1:%d/chat" % self.port)
        self.assertEqual(conn.state, STATE_OPEN)
        conn.send_message("hello")
        self.assertEqual(conn.recv_message(), "hello")
        conn.send_message(b"\x01\x02\x03")
        self.assertEqual(conn.recv_message(), b"\x01\x02\x03")
        conn.close(1000, "bye")
        self.assertEqual(conn.state, STATE_CLOSED)

    def test_large_message_over_two_byte_length(self):
        conn = connect("ws://127.0.0.1:%d/" % self.port)
        payload = "x" * 200000  # forces the 64-bit length encoding
        conn.send_message(payload, fragment_size=4096)
        self.assertEqual(conn.recv_message(), payload)
        conn.close()

    def test_fragmented_echo(self):
        conn = connect("ws://127.0.0.1:%d/" % self.port)
        conn.send_message("join me", fragment_size=2)
        self.assertEqual(conn.recv_message(), "join me")
        conn.close()

    def test_concurrent_clients(self):
        results = []
        errors = []

        def one_client(index):
            try:
                conn = connect("ws://127.0.0.1:%d/" % self.port)
                conn.send_message("client-%d" % index)
                results.append(conn.recv_message())
                conn.close()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=one_client, args=(i,))
                   for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results),
                         ["client-%d" % i for i in range(10)])

    def test_close_reaches_server_handler(self):
        conn = connect("ws://127.0.0.1:%d/" % self.port)
        conn.send_message("last")
        self.assertEqual(conn.recv_message(), "last")
        conn.close(1000, "done")
        self.assertEqual(conn.close_code, 1000)


class HandshakeFailureIntegrationTest(unittest.TestCase):
    def test_non_websocket_request_rejected(self):
        server = serve("127.0.0.1", 0, lambda conn: None)
        self.addCleanup(server.shutdown)
        port = server.address[1]
        import socket
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        response = sock.recv(4096)
        self.assertIn(b"400 Bad Request", response)
        sock.close()

    def test_wss_url_rejected(self):
        with self.assertRaises(HandshakeError):
            connect("wss://127.0.0.1:1/")


class ServerHeartbeatTest(unittest.TestCase):
    def test_server_heartbeat_with_live_client(self):
        server = serve("127.0.0.1", 0, self._hold,
                       ping_interval=0.05, ping_timeout=0.5)
        self.addCleanup(server.shutdown)
        port = server.address[1]
        conn = connect("ws://127.0.0.1:%d/" % port)
        # The client answers the server's pings while waiting.
        with self.assertRaises(TimeoutError):
            conn.recv_message(timeout=0.3)
        self.assertEqual(conn.state, STATE_OPEN)
        conn.close()

    @staticmethod
    def _hold(conn):
        try:
            conn.recv_message(timeout=0.6)
        except (TimeoutError, WebSocketError):
            pass


if __name__ == "__main__":
    unittest.main()
