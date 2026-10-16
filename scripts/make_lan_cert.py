"""Create a self-signed TLS certificate for LAN access to the review page (compose.lan.yaml).

Run inside the image so no host crypto tooling is needed:
  docker compose run --rm --no-deps -v ./.runtime/lan-tls:/out gateway \
      python scripts/make_lan_cert.py --ip 192.168.1.9 --out /out
Never overwrites an existing key. Prints only the certificate fingerprint, never the key.
"""

import argparse
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def make_certificate(ips, days=365):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local Privacy Gateway (LAN)")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips] + [x509.DNSName("localhost")]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert, cert.public_bytes(serialization.Encoding.PEM), key_pem


def fingerprint(cert):
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", action="append", required=True, help="LAN address; repeatable")
    parser.add_argument("--out", type=Path, default=Path(".runtime/lan-tls"))
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cert, cert_pem, key_pem = make_certificate(args.ip, args.days)
    with (args.out / "key.pem").open("xb") as stream:
        stream.write(key_pem)
    (args.out / "cert.pem").write_bytes(cert_pem)
    print("Certificate for", ", ".join(args.ip), "valid", args.days, "days")
    print("SHA-256 fingerprint:", fingerprint(cert))


if __name__ == "__main__":
    main()
