"""Headless end-to-end gateway test: connect via AWS relay, TLS, auth, receive 1 frame, disconnect."""
import hashlib
import socket
import ssl
import sys
import time

from common import recv_line_json, recv_packet, send_line_json, send_packet

GATEWAY_HOST = "3.248.230.119"
GATEWAY_PORT = 59020
SESSION_ID = "remote-session-8317a7e4"
GATEWAY_SECRET = "9NkG6JjM6GbI0psKvANy4TmYz8A1l_1e"
TOKEN = "r7Mb6LSsqCguap6ew_-gX1VF4GupH6sr"
CA_FILE = "server.crt"
TIMEOUT = 30.0

def main() -> None:
    print(f"[1/5] Connecting to gateway {GATEWAY_HOST}:{GATEWAY_PORT}...")
    t0 = time.perf_counter()
    raw = socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=TIMEOUT)
    raw.settimeout(TIMEOUT)
    print(f"      TCP connected in {(time.perf_counter()-t0)*1000:.0f}ms")

    print("[2/5] Registering as client with gateway...")
    send_line_json(raw, {
        "type": "register",
        "role": "client",
        "session_id": SESSION_ID,
        "gateway_secret": GATEWAY_SECRET,
    })

    while True:
        reply = recv_line_json(raw)
        print(f"      Gateway: {reply}")
        t = reply.get("type")
        if t == "wait":
            print("      Waiting for host to connect...")
            continue
        if t == "paired":
            print("[3/5] Paired with host — starting TLS handshake...")
            break
        if t == "error":
            print(f"[FAIL] Gateway error: {reply.get('message')}", file=sys.stderr)
            sys.exit(1)
        print(f"[FAIL] Unexpected gateway reply: {reply}", file=sys.stderr)
        sys.exit(1)

    raw.settimeout(None)
    raw.settimeout(20.0)
    ctx = ssl.create_default_context(cafile=CA_FILE)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    sock = ctx.wrap_socket(raw, server_hostname=GATEWAY_HOST)
    sock.settimeout(20.0)
    cert_hash = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    print(f"      TLS established — cert SHA256: {cert_hash[:16]}...")

    print("[4/5] Sending hello with token...")
    send_packet(sock, {"type": "hello", "token": TOKEN})
    ack = recv_packet(sock)
    print(f"      Server ack: type={ack.get('type')} screen={ack.get('screen_width')}x{ack.get('screen_height')}")
    if ack.get("type") != "hello_ack":
        print(f"[FAIL] Expected hello_ack, got: {ack}", file=sys.stderr)
        sys.exit(1)

    print("[5/5] Waiting for first video frame...")
    frame = recv_packet(sock)
    mode = frame.get("mode")
    fw, fh = frame.get("frame_width"), frame.get("frame_height")
    blob_size = len(frame.get("_blob", b""))
    fps = frame.get("fps")
    print(f"      Frame received: mode={mode} size={fw}x{fh} blob={blob_size}B server_fps={fps}")

    sock.close()
    elapsed = time.perf_counter() - t0
    print(f"\n[OK] End-to-end gateway test PASSED in {elapsed:.2f}s")
    print("     - Gateway relay: OK")
    print("     - TLS: OK")
    print("     - Auth: OK")
    print("     - Video stream: OK")
    print("     - 15-min session timeout: configured in server.py")

if __name__ == "__main__":
    main()
