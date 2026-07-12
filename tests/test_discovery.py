"""Descubrimiento mDNS — la lógica de estado, sin tocar multicast real.

La plomería zeroconf (announce/browse de verdad) no se puede testear en CI:
el multicast no existe en la mayoría de runners. Lo que sí se testea es todo
lo que decide algo: el parseo del announce, el eco propio, y el refresco de
direcciones de peers emparejados.
"""

import socket

from tmisc.config import Settings
from tmisc.discovery import PROTO, SERVICE, Discovery
from tmisc.identity import NodeIdentity
from tmisc.peers import Peer, PeerStore


def _identity(tmp_path, name):
    return NodeIdentity.load_or_create(str(tmp_path / name))


def _settings(tmp_path, port=8787):
    s = Settings(cache_dir=str(tmp_path / "cache"), port=port,
                 node_name="mi-pc", p2p=True).resolved()
    return s


def test_announce_lleva_la_identidad(tmp_path):
    ident = _identity(tmp_path, "a")
    d = Discovery(ident, _settings(tmp_path, port=9787))

    info = d.build_service_info()
    assert info.type == SERVICE
    assert info.port == 9788                            # peer_port = port + 1
    props = {k.decode(): v.decode() for k, v in info.properties.items()}
    assert props["id"] == ident.node_id
    assert props["proto"] == PROTO
    assert props["name"] == "mi-pc"
    assert len(info.addresses) == 1
    socket.inet_ntoa(info.addresses[0])                 # una IPv4 válida


def test_record_ignora_el_eco_propio(tmp_path):
    ident = _identity(tmp_path, "a")
    d = Discovery(ident, _settings(tmp_path))
    d.record("yo._tmisc._tcp.local.", ident.node_id, "mi-pc", "192.168.1.5", 8788)
    assert d.found == {}


def test_record_y_forget(tmp_path):
    ident = _identity(tmp_path, "a")
    otro = _identity(tmp_path, "b")
    d = Discovery(ident, _settings(tmp_path))

    d.record("otro._tmisc._tcp.local.", otro.node_id, "rocola", "192.168.1.9", 9788)
    assert d.found[otro.node_id] == {"name": "rocola", "host": "192.168.1.9",
                                     "port": 9788}
    d.forget("otro._tmisc._tcp.local.")
    assert d.found == {}


def test_peer_emparejado_cambia_de_ip(tmp_path):
    """La rocola se mudó de IP: el announce refresca peers.json solo."""
    ident = _identity(tmp_path, "a")
    otro = _identity(tmp_path, "b")
    store = PeerStore(str(tmp_path / "a"))
    store.add(Peer(node_id=otro.node_id, pubkey_hex=otro.pubkey_hex,
                   cert_pem=otro.cert_pem, name="rocola", addr="192.168.1.9:9788"))

    d = Discovery(ident, _settings(tmp_path), peers=store)
    d.record("otro._tmisc._tcp.local.", otro.node_id, "rocola", "192.168.1.40", 9788)

    assert store.get(otro.node_id).addr == "192.168.1.40:9788"
    # y quedó persistido, no solo en memoria
    assert PeerStore(str(tmp_path / "a")).get(otro.node_id).addr == "192.168.1.40:9788"


def test_desconocido_no_toca_el_store(tmp_path):
    ident = _identity(tmp_path, "a")
    otro = _identity(tmp_path, "b")
    store = PeerStore(str(tmp_path / "a"))

    d = Discovery(ident, _settings(tmp_path), peers=store)
    d.record("x._tmisc._tcp.local.", otro.node_id, "desconocido", "192.168.1.7", 9788)

    assert store.all() == []                            # descubierto ≠ confiado
    assert otro.node_id in d.found
