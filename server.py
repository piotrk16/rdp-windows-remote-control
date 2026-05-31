import argparse
import base64
import io
import socket
import ssl
import threading
import time
from typing import Dict, Optional

import mss
import pyautogui
from PIL import Image

from common import ProtocolError, recv_packet, send_packet


pyautogui.FAILSAFE = False


BUTTONS = {"left", "right", "middle"}


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remote desktop server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=59010, help="Bind port")
    parser.add_argument("--fps", type=float, default=12.0, help="Frames per second")
    parser.add_argument("--quality", type=int, default=55, help="JPEG quality 1-95")
    parser.add_argument("--scale", type=float, default=1.0, help="Resize factor for outgoing frames")
    parser.add_argument("--token", default="", help="Optional shared token")
    parser.add_argument("--certfile", required=True, help="TLS certificate file (PEM)")
    parser.add_argument("--keyfile", required=True, help="TLS private key file (PEM)")
    return parser.parse_args()


def perform_action(packet: Dict[str, object], screen_w: int, screen_h: int) -> None:
    action = packet.get("type")

    if action == "mouse_move":
        x = clamp(int(packet.get("x", 0)), 0, screen_w - 1)
        y = clamp(int(packet.get("y", 0)), 0, screen_h - 1)
        pyautogui.moveTo(x, y)
        return

    if action == "mouse_button":
        button = str(packet.get("button", "left")).lower()
        is_down = bool(packet.get("down", True))
        if button not in BUTTONS:
            return
        if is_down:
            pyautogui.mouseDown(button=button)
        else:
            pyautogui.mouseUp(button=button)
        return

    if action == "mouse_scroll":
        amount = int(packet.get("amount", 0))
        x = clamp(int(packet.get("x", 0)), 0, screen_w - 1)
        y = clamp(int(packet.get("y", 0)), 0, screen_h - 1)
        pyautogui.scroll(amount, x=x, y=y)
        return

    if action == "key_event":
        key = str(packet.get("key", "")).strip().lower()
        event = str(packet.get("event", "")).strip().lower()
        if not key:
            return
        if event == "down":
            pyautogui.keyDown(key)
        elif event == "up":
            pyautogui.keyUp(key)
        elif event == "press":
            pyautogui.press(key)


def client_sender_loop(
    client_sock: socket.socket,
    stop_event: threading.Event,
    fps: float,
    quality: int,
    scale: float,
) -> None:
    frame_interval = 1.0 / max(1.0, fps)
    quality = clamp(quality, 1, 95)
    scale = max(0.2, min(scale, 1.0))

    with mss.mss() as sct:
        monitor = sct.monitors[1]

        while not stop_event.is_set():
            started = time.perf_counter()
            shot = sct.grab(monitor)
            frame = Image.frombytes("RGB", shot.size, shot.rgb)

            if scale != 1.0:
                resized = (int(frame.width * scale), int(frame.height * scale))
                frame = frame.resize(resized, Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

            send_packet(
                client_sock,
                {
                    "type": "frame",
                    "width": frame.width,
                    "height": frame.height,
                    "jpeg": encoded,
                    "ts": time.time(),
                },
            )

            elapsed = time.perf_counter() - started
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)


def client_receiver_loop(
    client_sock: socket.socket,
    stop_event: threading.Event,
    screen_w: int,
    screen_h: int,
) -> None:
    while not stop_event.is_set():
        packet = recv_packet(client_sock)
        perform_action(packet, screen_w, screen_h)


def handle_client(
    client_sock: socket.socket,
    addr: tuple[str, int],
    fps: float,
    quality: int,
    scale: float,
    token: str,
) -> None:
    print(f"[+] Client connected: {addr[0]}:{addr[1]}")
    stop_event = threading.Event()
    sender_thread: Optional[threading.Thread] = None

    try:
        hello = recv_packet(client_sock)
        if hello.get("type") != "hello":
            raise ProtocolError("Expected hello packet")

        if token:
            if str(hello.get("token", "")) != token:
                send_packet(client_sock, {"type": "error", "message": "Authentication failed"})
                print("[!] Client authentication failed")
                return

        screen_w, screen_h = pyautogui.size()
        send_packet(
            client_sock,
            {
                "type": "hello_ack",
                "screen_width": screen_w,
                "screen_height": screen_h,
                "server_fps": fps,
                "server_scale": scale,
            },
        )

        sender_thread = threading.Thread(
            target=client_sender_loop,
            args=(client_sock, stop_event, fps, quality, scale),
            daemon=True,
        )
        sender_thread.start()

        client_receiver_loop(client_sock, stop_event, screen_w, screen_h)
    except (ConnectionError, OSError, ProtocolError) as exc:
        print(f"[!] Client disconnected ({addr[0]}:{addr[1]}): {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Client error ({addr[0]}:{addr[1]}): {exc}")
    finally:
        stop_event.set()
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client_sock.close()
        if sender_thread and sender_thread.is_alive():
            sender_thread.join(timeout=1.0)


def main() -> None:
    args = parse_args()
    print("Remote desktop server starting...")
    print(f"Listening (TLS) on {args.host}:{args.port}")
    if args.token:
        print("Token authentication is enabled")
    else:
        print("WARNING: no authentication token configured")

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(certfile=args.certfile, keyfile=args.keyfile)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((args.host, args.port))
        server_sock.listen(5)

        while True:
            raw_client_sock, addr = server_sock.accept()
            try:
                client_sock = tls_context.wrap_socket(raw_client_sock, server_side=True)
            except ssl.SSLError as exc:
                print(f"[!] TLS handshake failed ({addr[0]}:{addr[1]}): {exc}")
                raw_client_sock.close()
                continue
            thread = threading.Thread(
                target=handle_client,
                args=(client_sock, addr, args.fps, args.quality, args.scale, args.token),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    main()
