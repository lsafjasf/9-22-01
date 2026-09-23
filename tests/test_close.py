"""Closing handshake and post-close behavior (RFC 6455 section 7)."""

import struct
import threading
import unittest

from wsmini.exceptions import WebSocketError, ProtocolError
from wsmini.framing import (
    OPCODE_CLOSE, encode_frame, read_frame, encode_close_payload,
)
from wsmini.connection import (
    STATE_OPEN, STATE_CLOSING, STATE_CLOSED,
)

from util import make_pair, raw_pair
from test_framing import SocketReaderWrap


class CloseHandshakeTest(unittest.TestCase):
    def test_bidirectional_close_with_code_and_reason(self):
        client, server = make_pair()
        server_result = {}

        def serve():
            # Returns None on clean close; peer's code/reason recorded.
            server_result["msg"] = server.recv_message()
            server_result["code"] = server.close_code
            server_result["reason"] = server.close_reason

        thread = threading.Thread(target=serve)
        thread.start()
        client.close(1000, "all done")
        thread.join(timeout=2.0)

        self.assertIsNone(server_result["msg"])
        self.assertEqual(server_result["code"], 1000)
        self.assertEqual(server_result["reason"], "all done")
        # Both sides completed the handshake and are CLOSED.
        self.assertEqual(client.state, STATE_CLOSED)
        self.assertEqual(server.state, STATE_CLOSED)
        # The initiator also learns the peer's (echoed) code.
        self.assertEqual(client.close_code, 1000)

    def test_server_initiated_close(self):
        client, server = make_pair()
        client_result = {}

        def run_client():
            client_result["msg"] = client.recv_message()

        thread = threading.Thread(target=run_client)
        thread.start()
        server.close(1001, "going away")
        thread.join(timeout=2.0)
        self.assertIsNone(client_result["msg"])
        self.assertEqual(client.close_code, 1001)
        self.assertEqual(client.close_reason, "going away")
        self.assertEqual(client.state, STATE_CLOSED)
        self.assertEqual(server.state, STATE_CLOSED)

    def test_close_without_code(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_CLOSE, b"", mask=True))
        self.assertIsNone(server.recv_message())
        self.assertIsNone(server.close_code)
        self.assertEqual(server.close_reason, "")
        # The reply is a close frame (echo of the empty payload).
        reply = read_frame(SocketReaderWrap(raw))
        self.assertEqual(reply.opcode, OPCODE_CLOSE)
        raw.close()

    def test_close_frame_with_one_byte_payload_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_CLOSE, b"\x03", mask=True))
        with self.assertRaises(ProtocolError):
            server.recv_message()
        raw.close()

    def test_illegal_close_code_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_CLOSE,
                                 encode_close_payload(1006), mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        raw.close()

    def test_invalid_close_reason_utf8_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(
            OPCODE_CLOSE, struct.pack("!H", 1000) + b"\xff\xfe", mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1007)
        raw.close()


class PostCloseTest(unittest.TestCase):
    def test_no_data_frames_after_close_received(self):
        client, server = make_pair()

        def run_client():
            client.recv_message()  # consumes the close, replies, CLOSED

        thread = threading.Thread(target=run_client)
        thread.start()
        server.close()
        thread.join(timeout=2.0)
        with self.assertRaises(WebSocketError):
            client.send_message("too late")
        with self.assertRaises(WebSocketError):
            server.send_message("too late")

    def test_no_data_after_close_initiated(self):
        client, server = make_pair(close_timeout=0.2)
        client.close()
        self.assertEqual(client.state, STATE_CLOSED)
        with self.assertRaises(WebSocketError):
            client.send_message("nope")
        server.close()

    def test_close_is_idempotent(self):
        client, server = make_pair(close_timeout=0.2)
        client.close()
        client.close()  # second call is a no-op
        self.assertEqual(client.state, STATE_CLOSED)
        server.close()

    def test_invalid_close_code_value_rejected_locally(self):
        client, server = make_pair(close_timeout=0.2)
        with self.assertRaises(ValueError):
            client.close(1006)  # never legal on the wire
        with self.assertRaises(ValueError):
            client.close(999)
        client.close()
        server.close()


class CloseStateTransitionTest(unittest.TestCase):
    def test_observable_closing_state(self):
        client, server = make_pair()
        raw = server.sock  # silent peer: never answers the close frame
        client.close_timeout = 5.0
        states = []

        def run_close():
            client.close(1000, "bye")

        thread = threading.Thread(target=run_close)
        thread.start()
        # Wait until the close frame is on the wire; we must be CLOSING.
        frame = read_frame(SocketReaderWrap(raw))
        self.assertEqual(frame.opcode, OPCODE_CLOSE)
        states.append(client.state)
        # Now answer; the handshake completes and close() returns.
        raw.sendall(encode_frame(OPCODE_CLOSE, frame.payload, mask=False))
        thread.join(timeout=2.0)
        states.append(client.state)
        self.assertEqual(states, [STATE_CLOSING, STATE_CLOSED])
        self.assertEqual(client.close_code, 1000)
        self.assertEqual(client.close_reason, "bye")
        raw.close()


if __name__ == "__main__":
    unittest.main()
