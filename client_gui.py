import base64
import hashlib
import io
import socket
import ssl
import threading
import time
import tkinter as tk
from queue import Empty, Queue
from tkinter import messagebox, ttk
from typing import Optional

from PIL import Image, ImageTk

from common import ProtocolError, recv_packet, send_packet


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

        self.frame_queue: Queue[bytes] = Queue(maxsize=2)
        self.current_photo: Optional[ImageTk.PhotoImage] = None
        self.server_width = 1
        self.server_height = 1
        self.render_w = 1
        self.render_h = 1
        self.render_x = 0
        self.render_y = 0
        self.last_move_sent = 0.0

        self._build_ui()
        self.after(15, self._process_frame_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Host:").pack(side=tk.LEFT)
        self.host_var = tk.StringVar(value="127.0.0.1")
        ttk.Entry(top, textvariable=self.host_var, width=18).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="Port:").pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value="59010")
        ttk.Entry(top, textvariable=self.port_var, width=8).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="Token:").pack(side=tk.LEFT)
        self.token_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.token_var, width=20, show="*").pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="CA file:").pack(side=tk.LEFT)
        self.ca_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.ca_var, width=18).pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(top, text="Cert SHA256:").pack(side=tk.LEFT)
        self.fingerprint_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.fingerprint_var, width=24).pack(side=tk.LEFT, padx=(4, 12))

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

            self.sock = sock
            self.connected = True
            self.connect_btn.config(text="Disconnect")
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

                raw_b64 = packet.get("jpeg")
                if not isinstance(raw_b64, str):
                    continue

                frame_bytes = base64.b64decode(raw_b64)
                self.server_width = int(packet.get("width", self.server_width))
                self.server_height = int(packet.get("height", self.server_height))

                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except Empty:
                        pass
                self.frame_queue.put_nowait(frame_bytes)
        except (ConnectionError, OSError, ProtocolError):
            pass
        finally:
            self.after(0, lambda: self._disconnect("Disconnected"))

    def _process_frame_queue(self) -> None:
        latest: Optional[bytes] = None
        while True:
            try:
                latest = self.frame_queue.get_nowait()
            except Empty:
                break

        if latest:
            image = Image.open(io.BytesIO(latest)).convert("RGB")
            self._draw_frame(image)

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
