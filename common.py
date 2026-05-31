import json
import socket
import struct
from typing import Any, Dict


class ProtocolError(Exception):
    """Raised when the socket protocol stream is invalid."""


KIND_JSON = 1
KIND_JSON_BLOB = 2


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Socket closed")
        data.extend(chunk)
    return bytes(data)


def send_packet(sock: socket.socket, payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack("!BI", KIND_JSON, len(raw))
    sock.sendall(header)
    sock.sendall(raw)


def send_packet_with_blob(sock: socket.socket, payload: Dict[str, Any], blob: bytes) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = struct.pack("!BII", KIND_JSON_BLOB, len(raw), len(blob))
    sock.sendall(header)
    sock.sendall(raw)
    sock.sendall(blob)


def recv_packet(sock: socket.socket, max_size: int = 32 * 1024 * 1024) -> Dict[str, Any]:
    first = _recv_exact(sock, 1)
    (kind,) = struct.unpack("!B", first)

    if kind == KIND_JSON:
        header = _recv_exact(sock, 4)
        (size,) = struct.unpack("!I", header)
        if size <= 0 or size > max_size:
            raise ProtocolError(f"Invalid packet size: {size}")
        raw = _recv_exact(sock, size)
        blob = None
    elif kind == KIND_JSON_BLOB:
        header = _recv_exact(sock, 8)
        size, blob_size = struct.unpack("!II", header)
        if size <= 0 or size > max_size:
            raise ProtocolError(f"Invalid packet size: {size}")
        if blob_size < 0 or blob_size > max_size:
            raise ProtocolError(f"Invalid blob size: {blob_size}")
        raw = _recv_exact(sock, size)
        blob = _recv_exact(sock, blob_size)
    else:
        raise ProtocolError(f"Unknown packet kind: {kind}")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError("Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Packet payload must be a JSON object")
    if blob is not None:
        payload["_blob"] = blob
    return payload


def send_line_json(sock: socket.socket, payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    sock.sendall(raw)


def recv_line_json(sock: socket.socket, max_size: int = 16 * 1024) -> Dict[str, Any]:
    data = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("Socket closed")
        if chunk == b"\n":
            break
        data.extend(chunk)
        if len(data) > max_size:
            raise ProtocolError("Control line too large")

    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError("Invalid JSON control payload") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Control payload must be a JSON object")
    return payload
