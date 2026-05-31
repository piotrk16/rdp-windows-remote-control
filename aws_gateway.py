import argparse
import select
import socket
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from common import ProtocolError, recv_line_json, send_line_json


SessionKey = Tuple[str, str]


@dataclass
class Pair:
    host_sock: socket.socket
    client_sock: socket.socket
    done_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class Endpoint:
    sock: socket.socket
    addr: Tuple[str, int]
    role: str
    key: SessionKey
    paired_event: threading.Event = field(default_factory=threading.Event)
    pair: Optional[Pair] = None


class GatewayState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting_hosts: Dict[SessionKey, Endpoint] = {}
        self.waiting_clients: Dict[SessionKey, Endpoint] = {}

    def register(self, endpoint: Endpoint) -> tuple[Optional[Pair], Optional[Endpoint]]:
        with self.lock:
            if endpoint.role == "host":
                peer = self.waiting_clients.pop(endpoint.key, None)
                if peer is None:
                    self.waiting_hosts[endpoint.key] = endpoint
                    return None, None
                pair = Pair(host_sock=endpoint.sock, client_sock=peer.sock)
                endpoint.pair = pair
                peer.pair = pair
                endpoint.paired_event.set()
                peer.paired_event.set()
                return pair, peer

            peer = self.waiting_hosts.pop(endpoint.key, None)
            if peer is None:
                self.waiting_clients[endpoint.key] = endpoint
                return None, None
            pair = Pair(host_sock=peer.sock, client_sock=endpoint.sock)
            endpoint.pair = pair
            peer.pair = pair
            endpoint.paired_event.set()
            peer.paired_event.set()
            return pair, peer

    def remove_waiting(self, endpoint: Endpoint) -> None:
        with self.lock:
            table = self.waiting_hosts if endpoint.role == "host" else self.waiting_clients
            existing = table.get(endpoint.key)
            if existing is endpoint:
                table.pop(endpoint.key, None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Public TCP relay gateway for remote desktop")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=59020, help="Bind port")
    return parser.parse_args()


def validate_registration(packet: dict) -> tuple[str, SessionKey]:
    if packet.get("type") != "register":
        raise ProtocolError("Expected register packet")

    role = str(packet.get("role", "")).strip().lower()
    if role not in {"host", "client"}:
        raise ProtocolError("Invalid role")

    session_id = str(packet.get("session_id", "")).strip()
    gateway_secret = str(packet.get("gateway_secret", "")).strip()
    if not session_id or not gateway_secret:
        raise ProtocolError("session_id and gateway_secret are required")
    if len(session_id) > 128 or len(gateway_secret) > 256:
        raise ProtocolError("session_id or gateway_secret too long")

    return role, (session_id, gateway_secret)


def pipe(src: socket.socket, dst: socket.socket, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        stop.set()


def relay_pair(pair: Pair) -> None:
    stop = threading.Event()
    t = threading.Thread(target=pipe, args=(pair.host_sock, pair.client_sock, stop), daemon=True)
    t.start()
    pipe(pair.client_sock, pair.host_sock, stop)

    try:
        pair.host_sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        pair.client_sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    pair.host_sock.close()
    pair.client_sock.close()
    pair.done_event.set()


def wait_for_pair_or_disconnect(endpoint: Endpoint) -> Pair:
    while not endpoint.paired_event.wait(timeout=1.0):
        readable, _, exceptional = select.select([endpoint.sock], [], [endpoint.sock], 0)
        if exceptional:
            raise ConnectionError("Socket error while waiting for pair")
        if readable:
            try:
                probe = endpoint.sock.recv(1, socket.MSG_PEEK)
            except OSError as exc:
                raise ConnectionError("Socket closed while waiting for pair") from exc
            if not probe:
                raise ConnectionError("Socket closed while waiting for pair")

    if endpoint.pair is None:
        raise RuntimeError("Pairing failed unexpectedly")
    return endpoint.pair


def handle_connection(client_sock: socket.socket, addr: Tuple[str, int], state: GatewayState) -> None:
    endpoint: Optional[Endpoint] = None
    pair: Optional[Pair] = None

    try:
        client_sock.settimeout(120.0)
        packet = recv_line_json(client_sock)
        role, key = validate_registration(packet)
        endpoint = Endpoint(sock=client_sock, addr=addr, role=role, key=key)

        pair, peer = state.register(endpoint)
        if pair is None:
            send_line_json(client_sock, {"type": "wait"})
            pair = wait_for_pair_or_disconnect(endpoint)
        else:
            client_sock.settimeout(None)
            if peer is None:
                raise RuntimeError("Expected peer endpoint for completed pair")
            send_line_json(peer.sock, {"type": "paired"})
            send_line_json(client_sock, {"type": "paired"})
            threading.Thread(target=relay_pair, args=(pair,), daemon=True).start()

        client_sock.settimeout(None)
        pair.done_event.wait()
    except (ConnectionError, OSError, ProtocolError, RuntimeError) as exc:
        try:
            send_line_json(client_sock, {"type": "error", "message": str(exc)})
        except OSError:
            pass
    finally:
        if endpoint is not None and not endpoint.paired_event.is_set():
            state.remove_waiting(endpoint)
        if pair is None:
            try:
                client_sock.close()
            except OSError:
                pass


def main() -> None:
    args = parse_args()
    state = GatewayState()

    print("AWS gateway relay starting...")
    print(f"Listening on {args.host}:{args.port}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((args.host, args.port))
        server_sock.listen(200)

        while True:
            client_sock, addr = server_sock.accept()
            thread = threading.Thread(target=handle_connection, args=(client_sock, addr, state), daemon=True)
            thread.start()


if __name__ == "__main__":
    main()
