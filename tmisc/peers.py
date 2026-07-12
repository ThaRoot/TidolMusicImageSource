"""Peers confiados y protocolo de sesión entre nodos.

`peers.json` vive en el state_dir, NO en catalog.db: el catálogo es derivable
de los ficheros y borrarlo es siempre seguro; en quién confía este nodo no es
derivable de nada. Un puñado de peers no justifica SQLite: JSON legible,
escritura atómica con os.replace.

El handshake de sesión (peers YA emparejados, sobre TLS pineado):

  1. cliente -> POST /p2p/v1/session/challenge {node_id}
     servidor -> {challenge} si el node_id está emparejado; 403 si no.
  2. cliente -> POST /p2p/v1/session {node_id, challenge, sig}
     donde sig = Ed25519(challenge ‖ node_id_cliente ‖ node_id_servidor ‖
                          BLAKE3(cert_servidor))
     servidor -> {session, expires_in}

La firma incluye el cert del servidor tal y como lo ve el CLIENTE (channel
binding): una firma capturada no sirve contra otro nodo ni a través de otro
canal. El challenge es de un solo uso y caduca.
"""

import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field

from blake3 import blake3

from .identity import NodeIdentity, verify_signature

PEERS_FILE = "peers.json"

CHALLENGE_TTL = 60.0
SESSION_TTL = 3600.0

_SESSION_CONTEXT = b"tmisc-p2p-v1"


@dataclass
class Peer:
    node_id: str
    pubkey_hex: str
    cert_pem: str
    name: str = ""
    # "host:puerto" del listener P2P la última vez que se le vio. Fallback para
    # redes sin multicast; mDNS lo refresca cuando existe.
    addr: str = ""
    added_at: int = field(default_factory=lambda: int(time.time()))


class PeerStore:
    def __init__(self, state_dir: str) -> None:
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        self.path = os.path.join(state_dir, PEERS_FILE)
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
            self._peers = {p["node_id"]: Peer(**p) for p in raw.get("peers", [])}

    def _persist(self) -> None:
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"peers": [asdict(p) for p in self._peers.values()]},
                      fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def all(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def get(self, node_id: str) -> Peer | None:
        with self._lock:
            return self._peers.get(node_id)

    def add(self, peer: Peer) -> None:
        with self._lock:
            self._peers[peer.node_id] = peer
            self._persist()

    def remove(self, node_id: str) -> bool:
        with self._lock:
            if node_id not in self._peers:
                return False
            del self._peers[node_id]
            self._persist()
            return True

    def update_addr(self, node_id: str, addr: str) -> None:
        with self._lock:
            peer = self._peers.get(node_id)
            if peer is not None and peer.addr != addr:
                peer.addr = addr
                self._persist()

    def match(self, needle: str) -> Peer | None:
        """Un peer por prefijo de node_id o por nombre exacto (para la CLI)."""
        with self._lock:
            for p in self._peers.values():
                if p.node_id.startswith(needle) or p.name == needle:
                    return p
        return None


def session_message(challenge_hex: str, client_node_id: str,
                    server_node_id: str, server_cert_pem: str) -> bytes:
    """Lo que firma el cliente. Todos los campos son de longitud fija (32 B)."""
    return (_SESSION_CONTEXT
            + bytes.fromhex(challenge_hex)
            + bytes.fromhex(client_node_id)
            + bytes.fromhex(server_node_id)
            + blake3(server_cert_pem.encode()).digest())


class SessionManager:
    """Challenges de un solo uso y sesiones efímeras, en memoria.

    Los tokens se guardan hasheados: un volcado casual (log, repr) no filtra
    credenciales vivas. Revocar un peer invalida sus sesiones al instante.
    """

    def __init__(self, identity: NodeIdentity, peers: PeerStore,
                 session_ttl: float = SESSION_TTL) -> None:
        self.identity = identity
        self.peers = peers
        self.session_ttl = session_ttl
        self._lock = threading.Lock()
        self._challenges: dict[str, tuple[str, float]] = {}   # challenge -> (node_id, caducidad)
        self._sessions: dict[str, tuple[str, float]] = {}     # blake3(token) -> (node_id, caducidad)

    def new_challenge(self, node_id: str) -> str | None:
        """None si el node_id no está emparejado (el llamante devuelve 403)."""
        if self.peers.get(node_id) is None:
            return None
        challenge = secrets.token_hex(32)
        with self._lock:
            now = time.monotonic()
            self._challenges = {c: v for c, v in self._challenges.items() if v[1] > now}
            self._challenges[challenge] = (node_id, now + CHALLENGE_TTL)
        return challenge

    def redeem(self, node_id: str, challenge_hex: str, sig_hex: str) -> str | None:
        """Challenge + firma válidos -> token de sesión. None -> 401/403."""
        peer = self.peers.get(node_id)
        if peer is None:
            return None
        with self._lock:
            entry = self._challenges.pop(challenge_hex, None)   # un solo uso
        if entry is None or entry[0] != node_id or entry[1] <= time.monotonic():
            return None

        message = session_message(challenge_hex, node_id,
                                  self.identity.node_id, self.identity.cert_pem)
        try:
            sig = bytes.fromhex(sig_hex)
            pubkey = bytes.fromhex(peer.pubkey_hex)
        except ValueError:
            return None
        if not verify_signature(pubkey, message, sig):
            return None

        token = secrets.token_hex(32)
        with self._lock:
            self._sessions[blake3(token.encode()).hexdigest()] = (
                node_id, time.monotonic() + self.session_ttl,
            )
        return token

    def peer_for(self, token: str) -> str | None:
        """node_id dueño de la sesión, o None si no existe/caducó."""
        key = blake3(token.encode()).hexdigest()
        with self._lock:
            entry = self._sessions.get(key)
            if entry is None:
                return None
            if entry[1] <= time.monotonic():
                del self._sessions[key]
                return None
            return entry[0]

    def invalidate_peer(self, node_id: str) -> None:
        with self._lock:
            self._sessions = {k: v for k, v in self._sessions.items() if v[0] != node_id}
            self._challenges = {c: v for c, v in self._challenges.items() if v[0] != node_id}
