import json
import socket
import struct
from typing import Any, Dict


class ProtocolError(Exception):
    """Raised when the socket protocol stream is invalid."""


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
    header = struct.pack("!I", len(raw))
    sock.sendall(header)
    sock.sendall(raw)


def recv_packet(sock: socket.socket, max_size: int = 32 * 1024 * 1024) -> Dict[str, Any]:
    header = _recv_exact(sock, 4)
    (size,) = struct.unpack("!I", header)
    if size <= 0 or size > max_size:
        raise ProtocolError(f"Invalid packet size: {size}")
    raw = _recv_exact(sock, size)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError("Invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Packet payload must be a JSON object")
    return payload
