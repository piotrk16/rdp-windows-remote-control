import argparse
import io
import socket
import ssl
import threading
import time
from typing import Dict, Optional

import mss
import pyautogui
from PIL import Image, ImageChops

from common import ProtocolError, recv_line_json, recv_packet, send_line_json, send_packet, send_packet_with_blob


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
    parser.add_argument("--monitor", type=int, default=1, help="Monitor index to stream (1..N)")
    parser.add_argument("--list-monitors", action="store_true", help="List available monitors and exit")
    parser.add_argument("--keyframe-interval", type=int, default=60, help="Full keyframe interval in frames")
    parser.add_argument("--adaptive", action="store_true", help="Enable adaptive FPS/quality/scale")
    parser.add_argument("--min-fps", type=float, default=6.0, help="Adaptive minimum FPS")
    parser.add_argument("--max-fps", type=float, default=20.0, help="Adaptive maximum FPS")
    parser.add_argument("--min-scale", type=float, default=0.5, help="Adaptive minimum stream scale")
    parser.add_argument("--max-scale", type=float, default=1.0, help="Adaptive maximum stream scale")
    parser.add_argument("--min-quality", type=int, default=30, help="Adaptive minimum JPEG quality")
    parser.add_argument("--max-quality", type=int, default=75, help="Adaptive maximum JPEG quality")
    parser.add_argument(
        "--net-profile",
        choices=("balanced", "conservative", "aggressive", "custom"),
        default="balanced",
        help="Preset adaptive profile (custom keeps explicit CLI values)",
    )
    parser.add_argument("--gateway-host", default="", help="Public relay host (AWS)")
    parser.add_argument("--gateway-port", type=int, default=59020, help="Public relay port")
    parser.add_argument("--gateway-session", default="", help="Gateway session ID")
    parser.add_argument("--gateway-secret", default="", help="Gateway shared secret")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    gateway_enabled = bool(args.gateway_host.strip())
    if gateway_enabled:
        if not args.gateway_session.strip() or not args.gateway_secret.strip():
            raise ValueError("Gateway mode requires --gateway-session and --gateway-secret")


def list_monitors() -> None:
    with mss.mss() as sct:
        for idx, monitor in enumerate(sct.monitors[1:], start=1):
            print(
                f"Monitor {idx}: left={monitor['left']} top={monitor['top']} "
                f"width={monitor['width']} height={monitor['height']}"
            )


def get_monitor(index: int) -> Dict[str, int]:
    with mss.mss() as sct:
        monitors = sct.monitors[1:]
        if not monitors:
            raise RuntimeError("No monitors available")
        if index < 1 or index > len(monitors):
            raise ValueError(f"Invalid --monitor {index}, available 1..{len(monitors)}")
        monitor = monitors[index - 1]
        return {
            "left": int(monitor["left"]),
            "top": int(monitor["top"]),
            "width": int(monitor["width"]),
            "height": int(monitor["height"]),
            "index": index,
        }


def apply_network_profile(args: argparse.Namespace) -> Dict[str, float | int | bool]:
    settings: Dict[str, float | int | bool] = {
        "fps": float(args.fps),
        "quality": int(args.quality),
        "scale": float(args.scale),
        "adaptive": bool(args.adaptive),
        "min_fps": float(args.min_fps),
        "max_fps": float(args.max_fps),
        "min_scale": float(args.min_scale),
        "max_scale": float(args.max_scale),
        "min_quality": int(args.min_quality),
        "max_quality": int(args.max_quality),
    }

    if args.net_profile == "conservative":
        settings.update(
            {
                "fps": 10.0,
                "quality": 50,
                "scale": 0.8,
                "adaptive": True,
                "min_fps": 5.0,
                "max_fps": 14.0,
                "min_scale": 0.45,
                "max_scale": 0.9,
                "min_quality": 28,
                "max_quality": 60,
            }
        )
    elif args.net_profile == "balanced":
        settings.update(
            {
                "fps": 12.0,
                "quality": 55,
                "scale": 0.9,
                "adaptive": True,
                "min_fps": 6.0,
                "max_fps": 18.0,
                "min_scale": 0.5,
                "max_scale": 1.0,
                "min_quality": 30,
                "max_quality": 72,
            }
        )
    elif args.net_profile == "aggressive":
        settings.update(
            {
                "fps": 18.0,
                "quality": 62,
                "scale": 1.0,
                "adaptive": True,
                "min_fps": 10.0,
                "max_fps": 28.0,
                "min_scale": 0.7,
                "max_scale": 1.0,
                "min_quality": 40,
                "max_quality": 85,
            }
        )
    return settings


def _set_keepalive(sock: socket.socket, idle_sec: int = 30, interval_sec: int = 10, probes: int = 6) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle_sec)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_sec)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, probes)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_sec * 1000, interval_sec * 1000))
    except OSError:
        pass


def connect_gateway_host(args: argparse.Namespace) -> socket.socket:
    relay_sock = socket.create_connection((args.gateway_host, args.gateway_port), timeout=15.0)
    _set_keepalive(relay_sock)
    relay_sock.settimeout(15.0)
    print(f"[*] Connecting to gateway {args.gateway_host}:{args.gateway_port} as host")
    send_line_json(
        relay_sock,
        {
            "type": "register",
            "role": "host",
            "session_id": args.gateway_session,
            "gateway_secret": args.gateway_secret,
        },
    )

    while True:
        reply = recv_line_json(relay_sock)
        print(f"[*] Gateway reply: {reply}")
        msg_type = str(reply.get("type", ""))
        if msg_type == "wait":
            print("[*] Gateway connected. Waiting for remote client...")
            relay_sock.settimeout(None)  # wait indefinitely for a client to pair
            continue
        if msg_type == "paired":
            print("[+] Gateway paired with remote client")
            relay_sock.settimeout(None)
            return relay_sock
        if msg_type == "error":
            raise RuntimeError(str(reply.get("message", "Gateway registration failed")))
        raise ProtocolError(f"Unexpected gateway reply: {reply}")


def to_abs_coords(x: int, y: int, monitor: Dict[str, int]) -> tuple[int, int]:
    rel_x = clamp(x, 0, monitor["width"] - 1)
    rel_y = clamp(y, 0, monitor["height"] - 1)
    return monitor["left"] + rel_x, monitor["top"] + rel_y


def perform_action(packet: Dict[str, object], monitor: Dict[str, int]) -> None:
    action = packet.get("type")

    if action == "mouse_move":
        x, y = to_abs_coords(int(packet.get("x", 0)), int(packet.get("y", 0)), monitor)
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
        x, y = to_abs_coords(int(packet.get("x", 0)), int(packet.get("y", 0)), monitor)
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
    monitor: Dict[str, int],
    fps: float,
    quality: int,
    scale: float,
    keyframe_interval: int,
    adaptive: bool,
    min_fps: float,
    max_fps: float,
    min_scale: float,
    max_scale: float,
    min_quality: int,
    max_quality: int,
) -> None:
    current_fps = max(1.0, fps)
    current_quality = clamp(quality, 1, 95)
    current_scale = max(0.2, min(scale, 1.0))
    min_fps = max(1.0, min_fps)
    max_fps = max(min_fps, max_fps)
    min_scale = max(0.2, min(min_scale, 1.0))
    max_scale = max(min_scale, min(max_scale, 1.0))
    min_quality = clamp(min_quality, 1, 95)
    max_quality = clamp(max_quality, min_quality, 95)
    keyframe_interval = max(1, keyframe_interval)

    force_keyframe = True
    sent_frames = 0
    prev_frame: Optional[Image.Image] = None
    send_time_acc = 0.0
    frame_interval_acc = 0.0

    with mss.mss() as sct:
        while not stop_event.is_set():
            started = time.perf_counter()
            frame_interval = 1.0 / current_fps
            shot = sct.grab(monitor)
            frame = Image.frombytes("RGB", shot.size, shot.rgb)

            if current_scale != 1.0:
                resized = (int(frame.width * current_scale), int(frame.height * current_scale))
                frame = frame.resize(resized, Image.Resampling.LANCZOS)

            mode = "delta"
            patch = frame
            x = 0
            y = 0

            if force_keyframe or prev_frame is None or sent_frames % keyframe_interval == 0:
                mode = "keyframe"
                patch = frame
                x = 0
                y = 0
            else:
                diff_box = ImageChops.difference(frame, prev_frame).getbbox()
                if diff_box is None:
                    elapsed = time.perf_counter() - started
                    sleep_time = frame_interval - elapsed
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue

                x, y, x2, y2 = diff_box
                patch = frame.crop(diff_box)

                changed_area = (x2 - x) * (y2 - y)
                full_area = frame.width * frame.height
                if changed_area > int(full_area * 0.75):
                    mode = "keyframe"
                    patch = frame
                    x = 0
                    y = 0

            buffer = io.BytesIO()
            patch.save(buffer, format="JPEG", quality=current_quality, optimize=True)
            jpeg_bytes = buffer.getvalue()

            send_started = time.perf_counter()
            send_packet_with_blob(
                client_sock,
                {
                    "type": "frame",
                    "mode": mode,
                    "x": x,
                    "y": y,
                    "patch_width": patch.width,
                    "patch_height": patch.height,
                    "frame_width": frame.width,
                    "frame_height": frame.height,
                    "quality": current_quality,
                    "fps": current_fps,
                    "scale": current_scale,
                    "ts": time.time(),
                },
                jpeg_bytes,
            )
            send_elapsed = time.perf_counter() - send_started
            send_time_acc += send_elapsed
            frame_interval_acc += frame_interval

            prev_frame = frame
            sent_frames += 1
            force_keyframe = False

            if adaptive and sent_frames % 30 == 0:
                avg_send = send_time_acc / 30.0
                avg_interval = frame_interval_acc / 30.0

                degraded = False
                if avg_send > avg_interval * 0.9:
                    if current_quality > min_quality:
                        current_quality = max(min_quality, current_quality - 5)
                        degraded = True
                    elif current_scale > min_scale:
                        current_scale = max(min_scale, round(current_scale - 0.1, 2))
                        force_keyframe = True
                        degraded = True
                    elif current_fps > min_fps:
                        current_fps = max(min_fps, current_fps - 1.0)
                        degraded = True

                if not degraded and avg_send < avg_interval * 0.45:
                    if current_fps < max_fps:
                        current_fps = min(max_fps, current_fps + 1.0)
                    elif current_scale < max_scale:
                        current_scale = min(max_scale, round(current_scale + 0.1, 2))
                        force_keyframe = True
                    elif current_quality < max_quality:
                        current_quality = min(max_quality, current_quality + 5)

                send_time_acc = 0.0
                frame_interval_acc = 0.0

            elapsed = time.perf_counter() - started
            sleep_time = frame_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)


def client_receiver_loop(
    client_sock: socket.socket,
    stop_event: threading.Event,
    monitor: Dict[str, int],
) -> None:
    while not stop_event.is_set():
        packet = recv_packet(client_sock)
        perform_action(packet, monitor)


SESSION_TIMEOUT_SECONDS = 900.0  # 15 minutes


def handle_client(
    client_sock: socket.socket,
    addr: tuple[str, int],
    monitor: Dict[str, int],
    fps: float,
    quality: int,
    scale: float,
    keyframe_interval: int,
    adaptive: bool,
    min_fps: float,
    max_fps: float,
    min_scale: float,
    max_scale: float,
    min_quality: int,
    max_quality: int,
    token: str,
) -> None:
    print(f"[+] Client connected: {addr[0]}:{addr[1]}")
    stop_event = threading.Event()
    sender_thread: Optional[threading.Thread] = None

    def _session_timeout() -> None:
        print(f"[!] Session timeout ({SESSION_TIMEOUT_SECONDS:.0f}s) — disconnecting {addr[0]}:{addr[1]}")
        stop_event.set()
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timeout_timer = threading.Timer(SESSION_TIMEOUT_SECONDS, _session_timeout)
    timeout_timer.daemon = True
    timeout_timer.start()

    try:
        hello = recv_packet(client_sock)
        if hello.get("type") != "hello":
            raise ProtocolError("Expected hello packet")

        if token:
            if str(hello.get("token", "")) != token:
                send_packet(client_sock, {"type": "error", "message": "Authentication failed"})
                print("[!] Client authentication failed")
                return

        send_packet(
            client_sock,
            {
                "type": "hello_ack",
                "screen_width": monitor["width"],
                "screen_height": monitor["height"],
                "monitor_index": monitor["index"],
                "monitor_left": monitor["left"],
                "monitor_top": monitor["top"],
                "server_fps": fps,
                "server_scale": scale,
            },
        )

        sender_thread = threading.Thread(
            target=client_sender_loop,
            args=(
                client_sock,
                stop_event,
                monitor,
                fps,
                quality,
                scale,
                keyframe_interval,
                adaptive,
                min_fps,
                max_fps,
                min_scale,
                max_scale,
                min_quality,
                max_quality,
            ),
            daemon=True,
        )
        sender_thread.start()

        client_receiver_loop(client_sock, stop_event, monitor)
    except (ConnectionError, OSError, ProtocolError) as exc:
        print(f"[!] Client disconnected ({addr[0]}:{addr[1]}): {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Client error ({addr[0]}:{addr[1]}): {exc}")
    finally:
        timeout_timer.cancel()
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
    validate_args(args)

    if args.list_monitors:
        list_monitors()
        return

    monitor = get_monitor(args.monitor)
    stream_settings = apply_network_profile(args)

    print("Remote desktop server starting...")
    if args.token:
        print("Token authentication is enabled")
    else:
        print("WARNING: no authentication token configured")
    print(
        f"Streaming monitor {monitor['index']} "
        f"({monitor['width']}x{monitor['height']} at {monitor['left']},{monitor['top']})"
    )
    print(
        "Stream config: "
        f"profile={args.net_profile}, fps={stream_settings['fps']}, "
        f"quality={stream_settings['quality']}, scale={stream_settings['scale']}, "
        f"adaptive={stream_settings['adaptive']}"
    )

    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
    tls_context.load_cert_chain(certfile=args.certfile, keyfile=args.keyfile)

    if args.gateway_host:
        print(f"Using AWS gateway relay {args.gateway_host}:{args.gateway_port}")
        while True:
            raw_client_sock: Optional[socket.socket] = None
            try:
                raw_client_sock = connect_gateway_host(args)
                print("[*] Gateway connection paired, starting TLS handshake")
                try:
                    client_sock = tls_context.wrap_socket(raw_client_sock, server_side=True)
                    print("[+] TLS handshake completed on gateway socket")
                except ssl.SSLError as exc:
                    print(f"[!] TLS handshake failed on gateway socket: {exc}")
                    raise
                handle_client(
                    client_sock,
                    (f"gateway:{args.gateway_host}", args.gateway_port),
                    monitor,
                    float(stream_settings["fps"]),
                    int(stream_settings["quality"]),
                    float(stream_settings["scale"]),
                    args.keyframe_interval,
                    bool(stream_settings["adaptive"]),
                    float(stream_settings["min_fps"]),
                    float(stream_settings["max_fps"]),
                    float(stream_settings["min_scale"]),
                    float(stream_settings["max_scale"]),
                    int(stream_settings["min_quality"]),
                    int(stream_settings["max_quality"]),
                    args.token,
                )
            except (ConnectionError, OSError, ProtocolError, RuntimeError, ssl.SSLError) as exc:
                print(f"[!] Gateway session error: {exc}")
            finally:
                if raw_client_sock is not None:
                    try:
                        raw_client_sock.close()
                    except OSError:
                        pass
            time.sleep(2.0)

    print(f"Listening (TLS) on {args.host}:{args.port}")
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
                args=(
                    client_sock,
                    addr,
                    monitor,
                    float(stream_settings["fps"]),
                    int(stream_settings["quality"]),
                    float(stream_settings["scale"]),
                    args.keyframe_interval,
                    bool(stream_settings["adaptive"]),
                    float(stream_settings["min_fps"]),
                    float(stream_settings["max_fps"]),
                    float(stream_settings["min_scale"]),
                    float(stream_settings["max_scale"]),
                    int(stream_settings["min_quality"]),
                    int(stream_settings["max_quality"]),
                    args.token,
                ),
                daemon=True,
            )
            thread.start()


if __name__ == "__main__":
    main()
