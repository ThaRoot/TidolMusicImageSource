import io
import os
import shutil
import subprocess

import pytest

from tmisc.encode import encode_to_file
from tmisc.meta import TrackMeta

DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.fixture(scope="session")
def mp3_bytes() -> bytes:
    with open(os.path.join(DATA, "tone.mp3"), "rb") as fh:
        return fh.read()


@pytest.fixture(scope="session")
def flac_bytes() -> bytes:
    with open(os.path.join(DATA, "tone.flac"), "rb") as fh:
        return fh.read()


@pytest.fixture(scope="session")
def cover_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), (20, 60, 120)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def tmisc_v2(tmp_path, mp3_bytes, cover_bytes) -> str:
    """Un .tmisc v2 recién hecho, con metadatos."""
    out = str(tmp_path / "track.tmisc")
    encode_to_file(
        io.BytesIO(mp3_bytes), len(mp3_bytes), out,
        cover_data=cover_bytes,
        track=TrackMeta(title="Paranoid Android", artist="Radiohead", album="OK Computer"),
        source_filename="tone.mp3",
    )
    return out


@pytest.fixture
def v1_append(tmp_path) -> str:
    """Fixture congelado, generado con el código v1 ORIGINAL (ver git history)."""
    dst = str(tmp_path / "v1_append.tmisc")
    shutil.copy(os.path.join(DATA, "v1_append.tmisc"), dst)
    return dst


@pytest.fixture
def v1_lsb(tmp_path) -> str:
    dst = str(tmp_path / "v1_lsb.tmisc")
    shutil.copy(os.path.join(DATA, "v1_lsb.tmisc"), dst)
    return dst


@pytest.fixture
def library(tmp_path, mp3_bytes, flac_bytes, cover_bytes):
    """Biblioteca con v2 y v1 mezclados, ya escaneada."""
    from tmisc.catalog import Catalog
    from tmisc.scanner import scan

    lib = tmp_path / "lib"
    lib.mkdir()
    encode_to_file(io.BytesIO(mp3_bytes), len(mp3_bytes), str(lib / "a.tmisc"),
                   cover_data=cover_bytes,
                   track=TrackMeta(title="Paranoid Android", artist="Radiohead"))
    encode_to_file(io.BytesIO(flac_bytes), len(flac_bytes), str(lib / "b.tmisc"),
                   cover_data=cover_bytes,
                   track=TrackMeta(title="Xtal", artist="Aphex Twin"))
    shutil.copy(os.path.join(DATA, "v1_append.tmisc"), str(lib / "old.tmisc"))

    cat = Catalog(str(tmp_path / "catalog.db"))
    scan(str(lib), cat)
    return lib, cat


def has_ffmpeg() -> bool:
    return shutil.which("ffprobe") is not None


# --- nodos P2P completos (canal, pairing y pasarela) ---------------------------

class P2PKit:
    """Fábrica de nodos completos que conviven en el proceso.

    El transporte entre nodos es httpx.ASGITransport: el protocolo entero se
    ejercita in-process, sin sockets ni TLS. El TLS pineado de producción se
    valida aparte en test_identity.py con un handshake real.
    """

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self._by_addr = {}
        self.log = {}      # addr -> [(método, ruta) de cada petición que recibió]

    def node(self, name: str, tracks=()):
        from types import SimpleNamespace

        from server.app import create_app, create_peer_app
        from tmisc.catalog import Catalog
        from tmisc.config import PROFILE_NODE, Settings
        from tmisc.identity import NodeIdentity
        from tmisc.pairing import PairingManager
        from tmisc.peers import PeerStore, SessionManager
        from tmisc.scanner import scan

        root = self.tmp_path / name
        settings = Settings(
            library=str(root / "lib"), cache_dir=str(root / "cache"),
            profile=PROFILE_NODE, node_name=name, p2p=True,
        ).resolved()
        settings.ensure_dirs()

        for data, title in tracks:
            encode_to_file(io.BytesIO(data), len(data),
                           str(root / "lib" / f"{title}.tmisc"),
                           track=TrackMeta(title=title, artist=name))

        catalog = Catalog(settings.db_path)
        scan(settings.library, catalog)
        identity = NodeIdentity.load_or_create(settings.state_dir)
        peers = PeerStore(settings.state_dir)
        sessions = SessionManager(identity, peers)
        pairing = PairingManager(identity, peers, name, settings.effective_peer_port)
        # el "cableado" de la red de mentira: addr -> peer app del nodo destino
        pairing.client_factory = self.client_for

        from tmisc.peerclient import PeerNetwork
        network = PeerNetwork(identity, peers, catalog, settings,
                              transport_factory=self.transport_for)

        node = SimpleNamespace(
            name=name, settings=settings, catalog=catalog, identity=identity,
            peers=peers, sessions=sessions, pairing=pairing, network=network,
            app=create_app(settings, catalog=catalog, identity=identity,
                           peers=peers, sessions=sessions, network=network,
                           pairing=pairing),
        )
        node.peer_app = create_peer_app(settings, identity, peers, catalog,
                                        sessions=sessions, pairing=pairing)
        node.addr = f"{name}.local:9999"
        self._by_addr[node.addr] = node
        self.log.setdefault(node.addr, [])
        return node

    def transport_for(self, peer):
        """Transporte hacia el nodo dueño de esa identidad, con registro de
        peticiones: los tests cuentan qué endpoints remotos se tocaron."""
        import httpx

        target = next(n for n in self._by_addr.values()
                      if n.identity.node_id == peer.node_id)
        kit = self

        class RecordingTransport(httpx.ASGITransport):
            async def handle_async_request(self, request):
                kit.log[target.addr].append((request.method, request.url.path))
                return await super().handle_async_request(request)

        return RecordingTransport(app=target.peer_app, client=("127.0.0.1", 40000))

    def client_for(self, addr: str):
        """httpx.AsyncClient hacia la peer app del nodo con esa dirección."""
        import httpx

        target = self._by_addr[addr]
        return httpx.AsyncClient(
            base_url=f"https://{addr}",
            transport=httpx.ASGITransport(app=target.peer_app,
                                          client=("127.0.0.1", 40000)),
        )

    def pair(self, a, b) -> None:
        """Empareja a mano (el protocolo interactivo se testea en test_pairing)."""
        from tmisc.peers import Peer

        a.peers.add(Peer(node_id=b.identity.node_id, pubkey_hex=b.identity.pubkey_hex,
                         cert_pem=b.identity.cert_pem, name=b.name, addr=b.addr))
        b.peers.add(Peer(node_id=a.identity.node_id, pubkey_hex=a.identity.pubkey_hex,
                         cert_pem=a.identity.cert_pem, name=a.name, addr=a.addr))

    def client(self, a, b):
        """PeerClient de A hablando con B, in-process."""
        import httpx

        from tmisc.peerclient import PeerClient

        return PeerClient(a.identity, a.peers.get(b.identity.node_id),
                          addr=b.addr,
                          transport=httpx.ASGITransport(app=b.peer_app))


@pytest.fixture
def p2p(tmp_path):
    return P2PKit(tmp_path)
