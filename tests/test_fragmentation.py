"""Fragmentation and control-frame interleaving (RFC 6455 section 5.4)."""

import struct
import unittest

from wsmini.exceptions import ProtocolError
from wsmini.framing import (
    OPCODE_TEXT, OPCODE_BINARY, OPCODE_CONT, OPCODE_PING, OPCODE_PONG,
    OPCODE_CLOSE, encode_frame, read_frame, apply_mask,
)

from util import make_pair, raw_pair
from test_framing import SocketReaderWrap


class ReassemblyTest(unittest.TestCase):
    def test_fragmented_text_reassembled(self):
        client, server = make_pair(close_timeout=0.2)
        client.send_message("fragmented message", fragment_size=4)
        self.assertEqual(server.recv_message(), "fragmented message")
        client.close()
        server.close()

    def test_fragmented_binary_reassembled(self):
        client, server = make_pair(close_timeout=0.2)
        payload = bytes(range(256)) * 10
        client.send_message(payload, fragment_size=100)
        self.assertEqual(server.recv_message(), payload)
        client.close()
        server.close()

    def test_single_fragment_message(self):
        # fin=False followed by an empty final continuation is legal.
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_TEXT, b"abc", fin=False, mask=True))
        raw.sendall(encode_frame(OPCODE_CONT, b"", fin=True, mask=True))
        self.assertEqual(server.recv_message(), "abc")
        raw.close()

    def test_interleaved_control_frames(self):
        # A ping (and its pong) may sit between fragments of one message.
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_TEXT, b"hel", fin=False, mask=True))
        raw.sendall(encode_frame(OPCODE_PING, b"?", mask=True))
        raw.sendall(encode_frame(OPCODE_CONT, b"lo ", fin=False, mask=True))
        raw.sendall(encode_frame(OPCODE_PING, b"!", mask=True))
        raw.sendall(encode_frame(OPCODE_CONT, b"world", fin=True, mask=True))
        self.assertEqual(server.recv_message(), "hello world")
        # The server answered both pings with matching pongs.
        pong1 = read_frame(SocketReaderWrap(raw))
        pong2 = read_frame(SocketReaderWrap(raw))
        self.assertEqual((pong1.opcode, pong1.payload), (OPCODE_PONG, b"?"))
        self.assertEqual((pong2.opcode, pong2.payload), (OPCODE_PONG, b"!"))
        raw.close()


class FragmentationViolationTest(unittest.TestCase):
    def test_fragmented_control_frame_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_PING, b"x", fin=False, mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        reply = read_frame(SocketReaderWrap(raw))
        self.assertEqual(reply.opcode, OPCODE_CLOSE)
        self.assertEqual(struct.unpack("!H", reply.payload[:2])[0], 1002)
        raw.close()

    def test_continuation_without_start_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_CONT, b"orphan", mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        raw.close()

    def test_new_data_frame_mid_fragment_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_TEXT, b"start", fin=False,
                                 mask=True))
        raw.sendall(encode_frame(OPCODE_BINARY, b"intruder", mask=True))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        raw.close()


if __name__ == "__main__":
    unittest.main()
