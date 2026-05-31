from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_self_signed_cert(hostname: str, cert_path: Path, key_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Local Remote Desktop"),
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ]
    )

    alt_names = [x509.DNSName(hostname)]
    try:
        alt_names.append(x509.IPAddress(ip_address(hostname)))
    except ValueError:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main() -> None:
    cert_path = Path("server.crt")
    key_path = Path("server.key")
    hostname = "127.0.0.1"

    generate_self_signed_cert(hostname=hostname, cert_path=cert_path, key_path=key_path)
    print(f"Generated certificate: {cert_path.resolve()}")
    print(f"Generated key: {key_path.resolve()}")
    print("Use cert SHA256 fingerprint in client for pinning, or pass cert file as CA if trusted.")


if __name__ == "__main__":
    main()
