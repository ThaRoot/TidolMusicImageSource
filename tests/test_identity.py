"""Identidad Ed25519 del nodo y pinning TLS.

El test del handshake TLS real es el importante: valida que este OpenSSL
acepta certs firmados con Ed25519 y que el pinning (cert del peer como única
CA) acepta al peer bueno y rechaza a un impostor. Si esto pasa aquí, el canal
P2P no necesita más criptografía propia.
"""

import os
import socket
import ssl
import stat
import threading

from blake3 import blake3

from tmisc.identity import (
    NodeIdentity,
    node_id_for,
    pinned_ssl_context,
    pubkey_from_cert_pem,
    verify_signature,
)


def test_primera_carga_crea_y_persiste(tmp_path):
    d = str(tmp_path / "node")
    a = NodeIdentity.load_or_create(d)
    b = NodeIdentity.load_or_create(d)          # segunda carga: misma identidad

    assert a.node_id == b.node_id
    assert a.pubkey == b.pubkey
    assert len(a.pubkey) == 32
    assert a.node_id == blake3(a.pubkey).hexdigest()


def test_la_clave_privada_no_es_legible_por_otros(tmp_path):
    ident = NodeIdentity.load_or_create(str(tmp_path / "node"))
    mode = stat.S_IMODE(os.stat(ident.key_path).st_mode)
    assert mode == 0o600


def test_nodos_distintos_identidades_distintas(tmp_path):
    a = NodeIdentity.load_or_create(str(tmp_path / "a"))
    b = NodeIdentity.load_or_create(str(tmp_path / "b"))
    assert a.node_id != b.node_id


def test_cert_derivable_y_ligado_a_la_identidad(tmp_path):
    d = str(tmp_path / "node")
    a = NodeIdentity.load_or_create(d)
    os.unlink(a.cert_path)                       # borrar el cert no rompe nada
    b = NodeIdentity.load_or_create(d)

    assert a.cert_pem == b.cert_pem              # determinista: byte a byte
    # La clave dentro del cert ES la identidad. Sin esta comprobación, en el
    # pairing alguien podría anunciar la clave de otro con un cert propio.
    assert pubkey_from_cert_pem(a.cert_pem) == a.pubkey
    assert node_id_for(pubkey_from_cert_pem(a.cert_pem)) == a.node_id


def test_firma_y_verificacion(tmp_path):
    a = NodeIdentity.load_or_create(str(tmp_path / "a"))
    b = NodeIdentity.load_or_create(str(tmp_path / "b"))

    sig = a.sign(b"hola")
    assert verify_signature(a.pubkey, b"hola", sig)
    assert not verify_signature(a.pubkey, b"holA", sig)      # mensaje alterado
    assert not verify_signature(b.pubkey, b"hola", sig)      # otra identidad


def _handshake(server_ident: NodeIdentity, client_ctx: ssl.SSLContext) -> bool:
    """Handshake TLS real sobre un socketpair. True si el cliente lo acepta."""
    left, right = socket.socketpair()
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(server_ident.cert_path, server_ident.key_path)

    def serve() -> None:
        try:
            with server_ctx.wrap_socket(left, server_side=True) as s:
                s.recv(1)
        except (ssl.SSLError, OSError):
            pass                         # el rechazo del cliente aborta este lado

    t = threading.Thread(target=serve)
    t.start()
    try:
        with client_ctx.wrap_socket(right) as c:
            c.send(b"x")
        return True
    except ssl.SSLCertVerificationError:
        return False
    finally:
        t.join(timeout=5)


def test_pinning_acepta_al_peer_y_rechaza_al_impostor(tmp_path):
    a = NodeIdentity.load_or_create(str(tmp_path / "a"))
    impostor = NodeIdentity.load_or_create(str(tmp_path / "impostor"))

    assert _handshake(a, pinned_ssl_context(a.cert_pem)) is True
    # Mismo protocolo, cert autofirmado igual de válido — pero no es EL cert
    # pineado, así que el canal no se establece.
    assert _handshake(impostor, pinned_ssl_context(a.cert_pem)) is False
