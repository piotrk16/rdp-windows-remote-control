import socket, ssl, hashlib
from common import recv_line_json, send_line_json
host = '3.250.70.93'
port = 59020
session = 'remote-session-8317a7e4'
secret = '9NkG6JjM6GbI0psKvANy4TmYz8A1l_1e'
ca_file = 'server.crt'
print('Connecting to gateway...')
raw = socket.create_connection((host, port), timeout=10.0)
raw.settimeout(45.0)
send_line_json(raw, {'type':'register','role':'client','session_id':session,'gateway_secret':secret})
while True:
    reply = recv_line_json(raw)
    print('Gateway reply', reply)
    if reply.get('type') == 'wait':
        continue
    if reply.get('type') == 'paired':
        print('Paired, now doing TLS')
        break
    raise RuntimeError('Unexpected reply: %r' % reply)
raw.settimeout(None)
context = ssl.create_default_context(cafile=ca_file)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.check_hostname = False
context.verify_mode = ssl.CERT_REQUIRED
sock = context.wrap_socket(raw, server_hostname=host)
print('TLS established, peer cert hash', hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest())
sock.close()
print('Success')
