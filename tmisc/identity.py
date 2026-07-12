"""Identidad criptográfica del nodo.

Un nodo ES su par de claves Ed25519. Todo lo demás se deriva de ahí:

  node_id   BLAKE3-256 de los 32 bytes de la clave pública — la misma función
            de direccionamiento que usa el resto del formato para el audio.
  cert.pem  certificado X.509 autofirmado con ESA clave. El canal entre nodos
            es TLS normal con el cert del peer *pineado*: verificar el cert es
            verificar la identidad, sin CAs y sin crypto casera en el data path.

El certificado es DETERMINISTA (serial y fechas fijos + firmas Ed25519
deterministas): se puede regenerar byte a byte desde `identity.key`, así que el
pinning de un peer no se rompe aunque `cert.pem` se borre y se reconstruya.

A diferencia de `catalog.db`, este directorio NO es derivable de los ficheros:
borrarlo re-clavea el nodo y obliga a re-emparejar con todos los peers.
"""

import datetime
import os
import ssl

from blake3 import blake3
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID

KEY_FILE = "identity.key"
CERT_FILE = "cert.pem"

# Fechas fijas para que el cert sea reproducible. 9999-12-31 es el valor que
# RFC 5280 define como "sin caducidad bien definida".
_NOT_BEFORE = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_NOT_AFTER = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)


def node_id_for(pubkey: bytes) -> str:
    """node_id = BLAKE3(clave pública cruda), hex. 64 caracteres."""
    return blake3(pubkey).hexdigest()


def _build_cert(private_key: Ed25519PrivateKey) -> bytes:
    """Cert X.509 autofirmado y determinista para el listener P2P (PEM)."""
    pub_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    nid = node_id_for(pub_raw)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, nid)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        # Serial derivado de la clave: mismo nodo -> mismo cert, byte a byte.
        .serial_number(int.from_bytes(blake3(b"tmisc-cert-serial" + pub_raw).digest()[:16]) | 1)
        .not_valid_before(_NOT_BEFORE)
        .not_valid_after(_NOT_AFTER)
        # ca=True: el cert autofirmado es su propia ancla de confianza.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, algorithm=None)   # Ed25519: el algoritmo va implícito
    )
    return cert.public_bytes(serialization.Encoding.PEM)


class NodeIdentity:
    def __init__(self, private_key: Ed25519PrivateKey, state_dir: str) -> None:
        self._key = private_key
        self.state_dir = state_dir
        self.pubkey = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.node_id = node_id_for(self.pubkey)
        self.cert_pem = _build_cert(private_key).decode()

    # --- persistencia --------------------------------------------------------
    @classmethod
    def load_or_create(cls, state_dir: str) -> "NodeIdentity":
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        key_path = os.path.join(state_dir, KEY_FILE)

        if os.path.exists(key_path):
            with open(key_path, "rb") as fh:
                key = serialization.load_pem_private_key(fh.read(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{key_path} no contiene una clave Ed25519.")
        else:
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            tmp = key_path + ".part"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(pem)
            os.replace(tmp, key_path)

        ident = cls(key, state_dir)
        # El cert es derivable de la clave; se materializa porque uvicorn quiere
        # rutas de fichero (ssl_certfile/ssl_keyfile), no objetos.
        cert_path = os.path.join(state_dir, CERT_FILE)
        cert_bytes = ident.cert_pem.encode()
        if not os.path.exists(cert_path) or open(cert_path, "rb").read() != cert_bytes:
            tmp = cert_path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(cert_bytes)
            os.replace(tmp, cert_path)
        return ident

    @property
    def key_path(self) -> str:
        return os.path.join(self.state_dir, KEY_FILE)

    @property
    def cert_path(self) -> str:
        return os.path.join(self.state_dir, CERT_FILE)

    @property
    def pubkey_hex(self) -> str:
        return self.pubkey.hex()

    # --- firma ---------------------------------------------------------------
    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


def verify_signature(pubkey: bytes, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pubkey).verify(signature, message)
        return True
    except Exception:
        return False


def pubkey_from_cert_pem(cert_pem: str) -> bytes | None:
    """Clave Ed25519 cruda dentro de un cert, o None si no es Ed25519.

    En el pairing se comprueba que la clave del cert presentado coincide con la
    identidad anunciada: sin esto, alguien podría anunciar la clave de otro y
    presentar un cert propio.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        pub = cert.public_key()
        if not isinstance(pub, Ed25519PublicKey):
            return None
        return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except Exception:
        return None


def pinned_ssl_context(peer_cert_pem: str) -> ssl.SSLContext:
    """Contexto TLS de cliente anclado a UN cert concreto.

    El cert del peer (intercambiado en el pairing) es la única CA del contexto:
    cualquier otro certificado, aunque lo firme una CA real, se rechaza. El
    hostname da igual — la identidad es la clave, no el nombre.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=peer_cert_pem)
    return ctx
