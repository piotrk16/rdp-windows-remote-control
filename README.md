# Python Remote Desktop (Windows)

This project provides:

- `server.py`: console app that captures the server desktop and executes mouse/keyboard commands.
- `client_gui.py`: Tkinter GUI app that connects to the server and controls the remote desktop.

## Important Security Note

Traffic is encrypted with TLS.
You should still use a strong token and trusted network paths (LAN/VPN) for best safety.

## Features

- Live screen stream (JPEG over TCP)
- Mouse move, click and wheel
- Keyboard key press/release
- TLS-encrypted transport (TLS 1.2+)
- Token auth after TLS handshake

## Requirements

- Windows machine for server control
- Python 3.10+ recommended

Install dependencies:

```powershell
pip install -r requirements.txt
```

## Generate TLS Certificate

Create a local self-signed cert and key:

```powershell
python generate_cert.py
```

This creates:

- `server.crt`
- `server.key`

Get the certificate SHA256 fingerprint (for client pinning):

```powershell
python -c "import ssl,hashlib;d=open('server.crt','rb').read();print(hashlib.sha256(d).hexdigest())"
```

## Run Server

```powershell
python server.py --host 0.0.0.0 --port 59010 --fps 12 --quality 55 --scale 1.0 --token mySecretToken --certfile server.crt --keyfile server.key
```

Arguments:

- `--host`: bind address (`0.0.0.0` for all interfaces)
- `--port`: TCP port
- `--fps`: stream frame rate
- `--quality`: JPEG quality 1-95
- `--scale`: stream resize factor (`1.0` full size, `0.5` half)
- `--token`: optional token (empty means no auth)
- `--certfile`: TLS certificate PEM file
- `--keyfile`: TLS key PEM file

## Run Client GUI

```powershell
python client_gui.py
```

In GUI:

1. Enter server IP, port, and token.
2. Set TLS validation with one of these methods:
3. `CA file`: path to trusted cert/CA file.
4. `Cert SHA256`: server certificate fingerprint for pinning.
5. Click **Connect**.
6. Click inside the video area to focus keyboard input.

The client requires CA verification or fingerprint pinning before connecting.

## Usage Tips

- Start with `--scale 0.7 --quality 45 --fps 10` for slower networks.
- Keep both machines on the same LAN/VPN.
- If the server uses Windows Firewall, allow incoming TCP on chosen port.
