import hashlib
import io
import socket
import ssl
import threading
import time
import tkinter as tk
from queue import Empty, Queue
from tkinter import messagebox, ttk
from typing import Any, Optional

from PIL import Image, ImageTk

from common import ProtocolError, recv_line_json, recv_packet, send_line_json, send_packet


SPECIAL_KEYS = {
    "Return": "enter",
    "BackSpace": "backspace",
    "Escape": "esc",
    "Tab": "tab",
    "Delete": "delete",
    "Insert": "insert",
    "Home": "home",
    "End": "end",
    "Prior": "pageup",
    "Next": "pagedown",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "space": "space",
    "F1": "f1",
    "F2": "f2",
    "F3": "f3",
    "F4": "f4",
    "F5": "f5",
    "F6": "f6",
    "F7": "f7",
    "F8": "f8",
    "F9": "f9",
    "F10": "f10",
    "F11": "f11",
    "F12": "f12",
}


class RemoteDesktopClient(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Python Remote Desktop Client")
        self.geometry("1100x720")
        self.minsize(760, 500)

        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.socket_lock = threading.Lock()
        self.receiver_thread: Optional[threading.Thread] = None

        self.frame_queue: Queue[dict[str, Any]] = Queue(maxsize=2)
        self.current_photo: Optional[ImageTk.PhotoImage] = None
        self.remote_frame: Optional[Image.Image] = None
        self.server_width = 1
        self.server_height = 1
        self.render_w = 1
        self.render_h = 1
        self.render_x = 0
        self.render_y = 0
        self.last_move_sent = 0.0
        self.stats_lock = threading.Lock()
        self._reset_stats()

        self._build_ui()
        self.after(15, self._process_frame_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _reset_stats(self) -> None:
        self.stats_started = time.perf_counter()
        self.stats_last_tick = self.stats_started
        self.stats_last_bytes = 0
        self.stats_last_frames = 0
        self.stats_last_rendered = 0
        self.stats_tot_bytes = 0
        self.stats_tot_frames = 0
        self.stats_tot_rendered = 0
        self.stats_keyframes = 0
        self.stats_deltaframes = 0
        self.stats_queue_drops = 0
        self.stats_latency_ms = 0.0
        self.stats_server_fps = 0.0
        self.stats_server_scale = 0.0
        self.stats_server_quality = 0
        self.stats_view = {
            "rx_mbps": 0.0,
            "render_fps": 0.0,
            "frames": 0,
            "keyframes": 0,
            "deltaframes": 0,
            "queue_drops": 0,
            "latency_ms": 0.0,
            "server_fps": 0.0,
            "server_scale": 0.0,
            "server_quality": 0,
        }

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        top2 = ttk.Frame(self, padding=(8, 0, 8, 8))
        top2.pack(fill=tk.X)

        ttk.Label(top, text="Host:").pack(side=tk.LEFT)
        self.host_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(top, textvariable=self.host_var, width=18).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="59010")
        ttk.Entry(top, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="Token:").pack(side=tk.LEFT)
        self.token_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.token_var, width=20, show="*").pack(side=tk.LEFT, padx=(4, 12))

        self.gateway_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Use AWS Gateway", variable=self.gateway_var).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Label(top2, text="Gateway Session:").pack(side=tk.LEFT)
        self.gateway_session_var = tk.StringVar(value="")
        ttk.Entry(top2, textvariable=self.gateway_session_var, width=20).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top2, text="Gateway Secret:").pack(side=tk.LEFT)
        self.gateway_secret_var = tk.StringVar(value="")
        ttk.Entry(top2, textvariable=self.gateway_secret_var, width=20, show="*").pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top2, text="CA file:").pack(side=tk.LEFT)
        self.ca_var = tk.StringVar(value="")
        ttk.Entry(top2, textvariable=self.ca_var, width=18).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top2, text="Cert SHA256:").pack(side=tk.LEFT)
        self.fingerprint_var = tk.StringVar(value="")
        ttk.Entry(top2, textvariable=self.fingerprint_var, width=24).pack(side=tk.LEFT, padx=(4, 12))

        self.connect_btn = ttk.Button(top, text="Connect", command=self._toggle_connection)
        self.connect_btn.pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<ButtonPress-1>", lambda e: self._on_mouse_button(e, "left", True))
        self.canvas.bind("<ButtonRelease-1>", lambda e: self._on_mouse_button(e, "left", False))
        self.canvas.bind("<ButtonPress-3>", lambda e: self._on_mouse_button(e, "right", True))
        self.canvas.bind("<ButtonRelease-3>", lambda e: self._on_mouse_button(e, "right", False))
        self.canvas.bind("<ButtonPress-2>", lambda e: self._on_mouse_button(e, "middle", True))
        self.canvas.bind("<ButtonRelease-2>", lambda e: self._on_mouse_button(e, "middle", False))
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        self.bind("<KeyPress>", self._on_key_press)
        self.bind("<KeyRelease>", self._on_key_release)

        info = (
            "Use left/middle/right mouse buttons and wheel directly on the preview. "
            "Click inside the preview before typing keys."
        )
        ttk.Label(self, text=info, padding=6).pack(fill=tk.X)

    def _toggle_connection(self) -> None:
        if self.connected:
            self._disconnect("Disconnected")
            return
        self._connect()

    def _connect(self) -> None:
        host = self.host_var.get().strip()
        token = self.token_var.get().strip()
        ca_file = self.ca_var.get().strip()
        fingerprint = self.fingerprint_var.get().strip().lower().replace(":", "")
        use_gateway = bool(self.gateway_var.get())
        gateway_session = self.gateway_session_var.get().strip()
        gateway_secret = self.gateway_secret_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Port must be a number")
            return

        if not ca_file and not fingerprint:
            messagebox.showerror(
                "TLS validation required",
                "Provide a CA file path or a server certificate SHA256 fingerprint.",
            )
            return

        if use_gateway and (not gateway_session or not gateway_secret):
            messagebox.showerror(
                "Gateway configuration required",
                "Gateway mode requires session and secret.",
            )
            return

        try:
            if ca_file:
                context = ssl.create_default_context(cafile=ca_file)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.check_hostname = False
                context.verify_mode = ssl.CERT_REQUIRED
            else:
                # Fingerprint pinning mode: TLS is encrypted, then cert is validated manually.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            raw_sock = socket.create_connection((host, port), timeout=5.0)

            if use_gateway:
                raw_sock.settimeout(45.0)
                send_line_json(
                    raw_sock,
                    {
                        "type": "register",
                        "role": "client",
                        "session_id": gateway_session,
                        "gateway_secret": gateway_secret,
                    },
                )
                while True:
                    gateway_reply = recv_line_json(raw_sock)
                    reply_type = str(gateway_reply.get("type", ""))
                    if reply_type == "wait":
                        continue
                    if reply_type == "paired":
                        break
                    if reply_type == "error":
                        raise RuntimeError(str(gateway_reply.get("message", "Gateway rejected connection")))
                    raise RuntimeError(f"Unexpected gateway reply: {reply_type}")

            raw_sock.settimeout(None)
            sock = context.wrap_socket(raw_sock, server_hostname=host)
            sock.settimeout(None)

            if fingerprint:
                cert_bin = sock.getpeercert(binary_form=True)
                cert_hash = hashlib.sha256(cert_bin).hexdigest()
                if cert_hash != fingerprint:
                    raise RuntimeError("Server certificate fingerprint mismatch")

            send_packet(sock, {"type": "hello", "token": token})
            ack = recv_packet(sock)
            if ack.get("type") == "error":
                raise RuntimeError(str(ack.get("message", "Connection refused")))
            if ack.get("type") != "hello_ack":
                raise RuntimeError("Unexpected handshake response")

            self.server_width = int(ack.get("screen_width", 1))
            self.server_height = int(ack.get("screen_height", 1))
            self._reset_stats()

            self.sock = sock
            self.connected = True
            self.connect_btn.config(text="Disconnect")
            if use_gateway:
                self.status_var.set(f"Connected via gateway {host}:{port}")
            else:
                self.status_var.set(f"Connected to {host}:{port}")
            self.focus_force()
            self.canvas.focus_set()

            self.receiver_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.receiver_thread.start()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Connection error", str(exc))

    def _disconnect(self, status: str) -> None:
        self.connected = False
        self.connect_btn.config(text="Connect")
        self.status_var.set(status)

        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.remote_frame = None
        self._reset_stats()

    def _send(self, payload: dict) -> None:
        if not self.connected or not self.sock:
            return
        with self.socket_lock:
            try:
                send_packet(self.sock, payload)
            except OSError:
                self.after(0, lambda: self._disconnect("Disconnected"))

    def _receive_loop(self) -> None:
        assert self.sock is not None
        try:
            while self.connected:
                packet = recv_packet(self.sock)
                if packet.get("type") != "frame":
                    continue

                blob = packet.get("_blob")
                if not isinstance(blob, (bytes, bytearray)):
                    continue

                self.server_width = int(packet.get("frame_width", self.server_width))
                self.server_height = int(packet.get("frame_height", self.server_height))

                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except Empty:
                        pass
                    with self.stats_lock:
                        self.stats_queue_drops += 1
                packet["_blob"] = bytes(blob)

                with self.stats_lock:
                    self.stats_tot_bytes += len(packet["_blob"])
                    self.stats_tot_frames += 1
                    if str(packet.get("mode", "")).lower() == "keyframe":
                        self.stats_keyframes += 1
                    else:
                        self.stats_deltaframes += 1
                    self.stats_server_quality = int(packet.get("quality", self.stats_server_quality))
                    self.stats_server_scale = float(packet.get("scale", self.stats_server_scale))
                    self.stats_server_fps = float(packet.get("fps", self.stats_server_fps))
                    ts = packet.get("ts")
                    if isinstance(ts, (int, float)):
                        self.stats_latency_ms = max(0.0, (time.time() - float(ts)) * 1000.0)

                self.frame_queue.put_nowait(packet)
        except (ConnectionError, OSError, ProtocolError):
            pass
        finally:
            self.after(0, lambda: self._disconnect("Disconnected"))

    def _process_frame_queue(self) -> None:
        latest: Optional[dict[str, Any]] = None
        while True:
            try:
                latest = self.frame_queue.get_nowait()
            except Empty:
                break

        if latest:
            self._apply_frame_packet(latest)
            if self.remote_frame is not None:
                self._draw_frame(self.remote_frame)
                with self.stats_lock:
                    self.stats_tot_rendered += 1

        self._update_stats_view()

        self.after(15, self._process_frame_queue)

    def _draw_frame(self, image: Image.Image) -> None:
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())

        src_w, src_h = image.size
        scale = min(canvas_w / src_w, canvas_h / src_h)
        self.render_w = max(1, int(src_w * scale))
        self.render_h = max(1, int(src_h * scale))
        self.render_x = (canvas_w - self.render_w) // 2
        self.render_y = (canvas_h - self.render_h) // 2

        if (self.render_w, self.render_h) != image.size:
            image = image.resize((self.render_w, self.render_h), Image.Resampling.BILINEAR)

        self.current_photo = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(self.render_x, self.render_y, anchor=tk.NW, image=self.current_photo)
        self._draw_stats_overlay()

    def _update_stats_view(self) -> None:
        now = time.perf_counter()
        with self.stats_lock:
            interval = now - self.stats_last_tick
            if interval < 0.7:
                return

            frame_delta = self.stats_tot_frames - self.stats_last_frames
            byte_delta = self.stats_tot_bytes - self.stats_last_bytes
            rendered_delta = self.stats_tot_rendered - self.stats_last_rendered

            rx_mbps = (byte_delta * 8.0) / (interval * 1_000_000.0)
            render_fps = rendered_delta / interval

            self.stats_view = {
                "rx_mbps": rx_mbps,
                "render_fps": render_fps,
                "frames": self.stats_tot_frames,
                "keyframes": self.stats_keyframes,
                "deltaframes": self.stats_deltaframes,
                "queue_drops": self.stats_queue_drops,
                "latency_ms": self.stats_latency_ms,
                "server_fps": self.stats_server_fps,
                "server_scale": self.stats_server_scale,
                "server_quality": self.stats_server_quality,
            }

            self.stats_last_tick = now
            self.stats_last_frames = self.stats_tot_frames
            self.stats_last_bytes = self.stats_tot_bytes
            self.stats_last_rendered = self.stats_tot_rendered

    def _draw_stats_overlay(self) -> None:
        with self.stats_lock:
            stats = dict(self.stats_view)

        total_frames = max(1, int(stats["frames"]))
        key_ratio = 100.0 * float(stats["keyframes"]) / total_frames
        text = (
            f"RX {stats['rx_mbps']:.2f} Mbps | Render {stats['render_fps']:.1f} FPS | "
            f"Latency {stats['latency_ms']:.0f} ms\n"
            f"Frames {stats['frames']} (K {stats['keyframes']} / D {stats['deltaframes']} = {key_ratio:.1f}% K) | "
            f"Drops {stats['queue_drops']}\n"
            f"Server fps={stats['server_fps']:.1f} scale={stats['server_scale']:.2f} quality={int(stats['server_quality'])}"
        )

        pad = 10
        x0 = self.render_x + pad
        y0 = self.render_y + pad
        x1 = x0 + 560
        y1 = y0 + 64
        self.canvas.create_rectangle(x0, y0, x1, y1, fill="#111111", outline="#3a3a3a")
        self.canvas.create_text(x0 + 8, y0 + 8, anchor=tk.NW, fill="#f0f0f0", font=("Consolas", 10), text=text)

    def _apply_frame_packet(self, packet: dict[str, Any]) -> None:
        mode = str(packet.get("mode", "")).lower()
        blob = packet.get("_blob")
        if not isinstance(blob, (bytes, bytearray)):
            return

        patch = Image.open(io.BytesIO(bytes(blob))).convert("RGB")
        frame_w = int(packet.get("frame_width", patch.width))
        frame_h = int(packet.get("frame_height", patch.height))

        if mode == "keyframe" or self.remote_frame is None:
            if patch.size != (frame_w, frame_h):
                patch = patch.resize((frame_w, frame_h), Image.Resampling.BILINEAR)
            self.remote_frame = patch
            return

        x = int(packet.get("x", 0))
        y = int(packet.get("y", 0))
        patch_w = int(packet.get("patch_width", patch.width))
        patch_h = int(packet.get("patch_height", patch.height))

        if patch.size != (patch_w, patch_h):
            patch = patch.resize((patch_w, patch_h), Image.Resampling.BILINEAR)

        if self.remote_frame.size != (frame_w, frame_h):
            self.remote_frame = self.remote_frame.resize((frame_w, frame_h), Image.Resampling.BILINEAR)

        if x < 0 or y < 0:
            return
        if x + patch.width > self.remote_frame.width or y + patch.height > self.remote_frame.height:
            return

        self.remote_frame.paste(patch, (x, y))

    def _redraw(self) -> None:
        if self.current_photo is None:
            self.canvas.delete("all")

    def _canvas_to_remote(self, x: int, y: int) -> Optional[tuple[int, int]]:
        if self.render_w <= 0 or self.render_h <= 0:
            return None
        if x < self.render_x or y < self.render_y:
            return None
        if x >= self.render_x + self.render_w or y >= self.render_y + self.render_h:
            return None

        local_x = x - self.render_x
        local_y = y - self.render_y
        remote_x = int(local_x * self.server_width / self.render_w)
        remote_y = int(local_y * self.server_height / self.render_h)
        return remote_x, remote_y

    def _on_mouse_move(self, event: tk.Event) -> None:
        if not self.connected:
            return
        now = time.perf_counter()
        if now - self.last_move_sent < 0.02:
            return
        mapped = self._canvas_to_remote(event.x, event.y)
        if not mapped:
            return
        self.last_move_sent = now
        x, y = mapped
        self._send({"type": "mouse_move", "x": x, "y": y})

    def _on_mouse_button(self, event: tk.Event, button: str, is_down: bool) -> None:
        if not self.connected:
            return
        mapped = self._canvas_to_remote(event.x, event.y)
        if mapped:
            x, y = mapped
            self._send({"type": "mouse_move", "x": x, "y": y})
        self.canvas.focus_set()
        self._send({"type": "mouse_button", "button": button, "down": is_down})

    def _on_mouse_wheel(self, event: tk.Event) -> None:
        if not self.connected:
            return
        mapped = self._canvas_to_remote(event.x, event.y)
        if not mapped:
            return
        amount = int(event.delta / 120) * 120
        x, y = mapped
        self._send({"type": "mouse_scroll", "amount": amount, "x": x, "y": y})

    def _map_tk_key(self, event: tk.Event) -> Optional[str]:
        if event.keysym in SPECIAL_KEYS:
            return SPECIAL_KEYS[event.keysym]

        if len(event.char) == 1 and event.char.isprintable():
            ch = event.char
            if ch == " ":
                return "space"
            return ch.lower()
        return None

    def _on_key_press(self, event: tk.Event) -> None:
        if not self.connected:
            return
        key = self._map_tk_key(event)
        if key:
            self._send({"type": "key_event", "event": "down", "key": key})

    def _on_key_release(self, event: tk.Event) -> None:
        if not self.connected:
            return
        key = self._map_tk_key(event)
        if key:
            self._send({"type": "key_event", "event": "up", "key": key})

    def _on_close(self) -> None:
        self._disconnect("Disconnected")
        self.destroy()


if __name__ == "__main__":
    app = RemoteDesktopClient()
    app.mainloop()
