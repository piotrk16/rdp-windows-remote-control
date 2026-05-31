# Python Remote Desktop (Windows)

This project provides:

- `server.py`: console app that captures the server desktop and executes mouse/keyboard commands.
- `client_gui.py`: Tkinter GUI app that connects to the server and controls the remote desktop.
- `aws_gateway.py`: public TCP relay (deploy on AWS EC2) for cross-country/NAT traversal.

## Important Security Note

Traffic is encrypted with TLS.
You should still use a strong token and trusted network paths (LAN/VPN) for best safety.

## Features

- Live screen stream (JPEG over TCP)
- Mouse move, click and wheel
- Keyboard key press/release
- TLS-encrypted transport (TLS 1.2+)
- Token auth after TLS handshake
- AWS relay mode (both sides connect outbound to public gateway)
- Binary frame transport (no base64 overhead)
- Delta frame updates with periodic keyframes
- Optional adaptive FPS/quality/scale tuning
- Monitor selection for multi-monitor hosts
- Built-in network profiles (`conservative`, `balanced`, `aggressive`)
- Live client telemetry overlay (bandwidth, FPS, latency, keyframe/delta)

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

Recommended low-bandwidth mode:

```powershell
python server.py --host 0.0.0.0 --port 59010 --fps 10 --quality 50 --scale 0.8 --adaptive --min-fps 6 --max-fps 18 --min-scale 0.5 --max-scale 1.0 --keyframe-interval 60 --monitor 1 --token mySecretToken --certfile server.crt --keyfile server.key
```

Profile-based mode (recommended):

```powershell
python server.py --host 0.0.0.0 --port 59010 --net-profile balanced --keyframe-interval 60 --monitor 1 --token mySecretToken --certfile server.crt --keyfile server.key
```

Profiles:

- `conservative`: for slower links (prefers lower bandwidth).
- `balanced`: default internet profile.
- `aggressive`: for fast links (higher quality/FPS).
- `custom`: use explicit `--fps/--quality/--scale` and adaptive bounds from CLI.

Server via AWS gateway (desktop behind NAT):

```powershell
python server.py --token mySecretToken --certfile server.crt --keyfile server.key --gateway-host <AWS_PUBLIC_IP_OR_DNS> --gateway-port 59020 --gateway-session teamA-session1 --gateway-secret longGatewaySecret
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
- `--gateway-host`: public relay host (enables gateway mode)
- `--gateway-port`: public relay TCP port
- `--gateway-session`: shared session identifier used by host and client
- `--gateway-secret`: shared secret used by host and client
- `--monitor`: monitor index to stream (`1..N`)
- `--list-monitors`: print available monitors and exit
- `--keyframe-interval`: force full keyframe every N frames
- `--adaptive`: enable automatic bandwidth adaptation
- `--net-profile`: apply preset stream profile (`balanced` default)
- `--min-fps` / `--max-fps`: adaptive fps range
- `--min-scale` / `--max-scale`: adaptive scale range
- `--min-quality` / `--max-quality`: adaptive JPEG quality range

## Run AWS Gateway

Run this on a public AWS EC2 instance:

```powershell
python aws_gateway.py --host 0.0.0.0 --port 59020
```

AWS notes:

- Open inbound TCP `59020` in EC2 Security Group.
- Use a static Elastic IP or DNS name.
- Both desktops only need outbound internet access.

## Run Client GUI

```powershell
python client_gui.py
```

In GUI:

1. For direct mode: enter server IP and server port.
2. For AWS mode: enter gateway host and gateway port (`59020`), then enable `Use AWS Gateway`.
3. In AWS mode provide the same `Gateway Session` and `Gateway Secret` as server.
4. Enter remote control `Token`.
5. Set TLS validation with one of these methods:
6. `CA file`: path to trusted cert/CA file.
7. `Cert SHA256`: server certificate fingerprint for pinning.
8. Click **Connect**.
9. Click inside the video area to focus keyboard input.

The client requires CA verification or fingerprint pinning before connecting.

Client telemetry overlay:

- Top-left overlay shows receive bandwidth, render FPS, end-to-end frame latency,
  keyframe/delta ratio, queue drops, and current server adaptive settings.

## Architecture (Gateway Mode)

`client_gui.py` -> public `aws_gateway.py` <- `server.py`

- Client and server each make outbound TCP connection to gateway.
- Gateway pairs connections using `(session_id, gateway_secret)`.
- After pairing, gateway becomes a raw byte relay.
- TLS handshake happens end-to-end between client and server through relay.

## Usage Tips

- Start with `--scale 0.7 --quality 45 --fps 10` for slower networks.
- Keep both machines on the same LAN/VPN.
- If the server uses Windows Firewall, allow incoming TCP on chosen port.
