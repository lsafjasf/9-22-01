"""Frame-layer behavior: masking, length encoding, malformed frames."""

import io
import struct
import unittest

from wsmini.exceptions import ProtocolError, WebSocketError
from wsmini.framing import (
    OPCODE_TEXT, OPCODE_BINARY, OPCODE_PING, OPCODE_CLOSE,
    encode_frame, read_frame, apply_mask,
    encode_close_payload,
)
from wsmini.connection import STATE_CLOSED

from util import make_pair, raw_pair


class MaskTest(unittest.TestCase):
    def test_mask_roundtrip(self):
        key = b"\x01\x02\x03\x04"
        data = bytes(range(256))
        masked = apply_mask(data, key)
        self.assertNotEqual(masked, data)
        self.assertEqual(apply_mask(masked, key), data)

    def test_empty_mask_key_is_noop(self):
        self.assertEqual(apply_mask(b"abc", b""), b"abc")


class LengthEncodingTest(unittest.TestCase):
    def roundtrip(self, size):
        payload = bytes(i % 251 for i in range(size))
        wire = encode_frame(OPCODE_BINARY, payload, mask=True)
        frame = read_frame(io.BytesIO(wire))
        self.assertEqual(frame.payload, payload)
        self.assertTrue(frame.masked)

    def test_small_payload_two_byte_header(self):
        wire = encode_frame(OPCODE_BINARY, b"x" * 125)
        self.assertEqual(wire[1] & 0x7F, 125)
        self.roundtrip(125)

    def test_16bit_length(self):
        wire = encode_frame(OPCODE_BINARY, b"x" * 126)
        self.assertEqual(wire[1] & 0x7F, 126)
        self.roundtrip(126)
        self.roundtrip(65535)

    def test_64bit_length(self):
        wire = encode_frame(OPCODE_BINARY, b"x" * 65536)
        self.assertEqual(wire[1] & 0x7F, 127)
        self.roundtrip(65536)
        self.roundtrip(200000)

    def test_high_bit_length_rejected(self):
        wire = bytes([0x82, 0x7F]) + struct.pack("!Q", 1 << 63)
        with self.assertRaises(ProtocolError):
            read_frame(io.BytesIO(wire))

    def test_max_payload_guard(self):
        wire = encode_frame(OPCODE_BINARY, b"x" * 1000)
        with self.assertRaises(ProtocolError) as ctx:
            read_frame(io.BytesIO(wire), max_payload=999)
        self.assertEqual(ctx.exception.close_code, 1009)


class MaskingEnforcementTest(unittest.TestCase):
    def test_server_rejects_unmasked_client_frame(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(OPCODE_TEXT, b"hi", mask=False))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        self.assertEqual(server.state, STATE_CLOSED)
        # The peer is told why: a masked close frame carrying 1002.
        reply = read_frame(SocketReaderWrap(raw))
        self.assertEqual(reply.opcode, OPCODE_CLOSE)
        self.assertEqual(struct.unpack("!H", reply.payload[:2])[0], 1002)
        raw.close()

    def test_client_rejects_masked_server_frame(self):
        client, server = make_pair()
        raw = server.sock  # raw peer socket of the client connection
        raw.sendall(encode_frame(OPCODE_TEXT, b"hi", mask=True))
        with self.assertRaises(ProtocolError):
            client.recv_message()
        raw.close()
        client.close()


class SocketReaderWrap:
    """Adapt a raw socket to the reader protocol for read_frame."""

    def __init__(self, sock):
        self.sock = sock

    def read(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                break
            data += chunk
        return data


class MalformedFrameTest(unittest.TestCase):
    def test_reserved_opcode_rejected(self):
        raw, server = raw_pair()
        raw.sendall(encode_frame(0x3, b"x", mask=True) if False
                    else bytes([0x83, 0x81, 1, 2, 3, 4]) + apply_mask(b"x", b"\x01\x02\x03\x04"))
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        raw.close()

    def test_rsv_bits_rejected(self):
        raw, server = raw_pair()
        payload = apply_mask(b"x", b"\x01\x02\x03\x04")
        raw.sendall(bytes([0xC1, 0x81]) + b"\x01\x02\x03\x04" + payload)
        with self.assertRaises(ProtocolError):
            server.recv_message()
        raw.close()

    def test_control_payload_over_125_rejected(self):
        raw, server = raw_pair()
        key = b"\x05\x06\x07\x08"
        payload = apply_mask(b"p" * 126, key)
        raw.sendall(bytes([0x89, 0xFE]) + struct.pack("!H", 126) + key + payload)
        with self.assertRaises(ProtocolError) as ctx:
            server.recv_message()
        self.assertEqual(ctx.exception.close_code, 1002)
        raw.close()

    def test_encode_rejects_reserved_opcode(self):
        with self.assertRaises(ValueError):
            encode_frame(0x5, b"x")

    def test_encode_rejects_oversized_control_payload(self):
        with self.assertRaises(ValueError):
            encode_frame(OPCODE_PING, b"p" * 126)


class EndToEndFramingTest(unittest.TestCase):
    def test_text_and_binary_types(self):
        client, server = make_pair(close_timeout=0.2)
        client.send_message("héllo")
        client.send_message(b"\x00\x01\x02")
        self.assertEqual(server.recv_message(), "héllo")
        self.assertEqual(server.recv_message(), b"\x00\x01\x02")
        client.close()
        server.close()

    def test_client_frames_are_masked_on_wire(self):
        client, server = make_pair(close_timeout=0.2)
        client.send_message("check the wire")
        frame = read_frame(SocketReaderWrap(server.sock))
        self.assertTrue(frame.masked)
        self.assertEqual(frame.payload, b"check the wire")
        client.close()
        server.close()

    def test_server_frames_are_unmasked_on_wire(self):
        client, server = make_pair(close_timeout=0.2)
        server.send_message("plain")
        frame = read_frame(SocketReaderWrap(client.sock))
        self.assertFalse(frame.masked)
        client.close()
        server.close()


if __name__ == "__main__":
    unittest.main()
