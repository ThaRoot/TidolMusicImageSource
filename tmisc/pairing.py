"""Pairing con código corto (SAS), estilo "Numeric Comparison" de Bluetooth.

Objetivo: que dos nodos intercambien claves públicas + certs a través de una
red que NO es de fiar todavía, y que un humano ancle esa confianza comparando
un código de 6 dígitos en ambas pantallas.

    A (iniciador)                       B (receptor, en ventana --accept)
    ------------------------------------------------------------------
    start:  commit=BLAKE3(nonce_A),
            identidad_A            ->
                                   <-   identidad_B, nonce_B
    reveal: nonce_A                ->      (B comprueba el commit)

    SAS = BLAKE3("tmisc-pair-v1" ‖ min(pkA,pkB) ‖ max(pkA,pkB)
                 ‖ nonce_A ‖ nonce_B ‖ BLAKE3(cert_B))[:4] % 10^6

Por qué 6 dígitos bastan: A se COMPROMETE con su nonce antes de ver el de B, y
B entrega su nonce antes de conocer el de A. Un hombre-en-medio mantiene dos
sesiones (A<->M, M<->B) y necesita que ambos SAS coincidan, pero en cada sesión
hay un nonce que aún no conoce cuando tiene que fijar el suyo: no puede moler.
Probabilidad por intento: 10⁻⁶, y cada intento quema una ventana de accept.

La confirmación es humana y en AMBOS lados; solo entonces cada nodo persiste al
otro en peers.json.
"""

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx
from blake3 import blake3

from .identity import NodeIdentity, node_id_for, pubkey_from_cert_pem
from .peers import Peer, PeerStore

PAIRING_TTL = 120.0
_SAS_CONTEXT = b"tmisc-pair-v1"


def compute_sas(pk_initiator_hex: str, pk_receiver_hex: str,
                nonce_initiator_hex: str, nonce_receiver_hex: str,
                receiver_cert_pem: str) -> str:
    lo, hi = sorted([bytes.fromhex(pk_initiator_hex), bytes.fromhex(pk_receiver_hex)])
    digest = blake3(
        _SAS_CONTEXT + lo + hi
        + bytes.fromhex(nonce_initiator_hex)
        + bytes.fromhex(nonce_receiver_hex)
        + blake3(receiver_cert_pem.encode()).digest()
    ).digest()
    return f"{int.from_bytes(digest[:4]) % 1_000_000:06d}"


def _valid_announcement(node_id: str, pubkey_hex: str, cert_pem: str) -> bool:
    """La identidad anunciada tiene que ser internamente coherente: el node_id
    es el hash de la clave y la clave del cert ES esa clave. Sin esto, alguien
    podría anunciar la clave de otro con un cert propio."""
    try:
        pub = bytes.fromhex(pubkey_hex)
    except ValueError:
        return False
    return (len(pub) == 32
            and node_id_for(pub) == node_id
            and pubkey_from_cert_pem(cert_pem) == pub)


@dataclass
class PairingSession:
    sid: str
    role: str                      # "initiator" | "receiver"
    peer_node_id: str = ""
    peer_pubkey_hex: str = ""
    peer_cert_pem: str = ""
    peer_name: str = ""
    peer_addr: str = ""
    own_nonce: str = ""
    peer_commit: str = ""          # solo receiver: el commit del iniciador
    sas: str | None = None
    local_confirmed: bool = False
    state: str = "sas"             # sas -> done | failed
    expires_at: float = field(default_factory=lambda: time.monotonic() + PAIRING_TTL)

    @property
    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def public_view(self) -> dict:
        return {
            "sid": self.sid,
            "role": self.role,
            "sas": self.sas,
            "state": "failed" if (self.expired and self.state != "done") else self.state,
            "local_confirmed": self.local_confirmed,
            "peer": {"node_id": self.peer_node_id, "name": self.peer_name,
                     "addr": self.peer_addr},
        }


class PairingManager:
    """Una sesión de pairing a la vez, con ventana de accept explícita.

    Fuera de la ventana, los endpoints /p2p/v1/pair/* devuelven 403: nadie
    puede hacerte aparecer prompts de pairing sin que tú hayas pedido estar
    aceptando. Un intento fallido consume la ventana.
    """

    def __init__(self, identity: NodeIdentity, peers: PeerStore, node_name: str,
                 peer_port: int) -> None:
        self.identity = identity
        self.peers = peers
        self.node_name = node_name
        self.peer_port = peer_port
        self._lock = threading.Lock()
        self._accept_until = 0.0
        self._session: PairingSession | None = None
        # Inyectable en tests (ASGITransport hacia la peer app del otro nodo).
        # verify=False a propósito: aún no hay confianza; la ancla el SAS.
        self.client_factory = lambda addr: httpx.AsyncClient(
            base_url=f"https://{addr}", verify=False, timeout=5.0)

    # --- receptor (lado --accept) ---------------------------------------------
    def open_accept(self, window: float = PAIRING_TTL) -> None:
        with self._lock:
            self._accept_until = time.monotonic() + window
            self._session = None

    def accept_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._accept_until

    def receive_start(self, payload: dict, client_host: str) -> dict | None:
        node_id = str(payload.get("node_id", ""))
        pubkey_hex = str(payload.get("pubkey", ""))
        cert_pem = str(payload.get("cert_pem", ""))
        commit = str(payload.get("commit", ""))
        if not _valid_announcement(node_id, pubkey_hex, cert_pem) or len(commit) != 64:
            return None

        session = PairingSession(
            sid=uuid.uuid4().hex,
            role="receiver",
            peer_node_id=node_id,
            peer_pubkey_hex=pubkey_hex,
            peer_cert_pem=cert_pem,
            peer_name=str(payload.get("name", "")),
            peer_addr=f"{client_host}:{payload.get('peer_port', 0)}" if client_host else "",
            own_nonce=secrets.token_hex(32),
            peer_commit=commit,
        )
        with self._lock:
            # una sesión por ventana: el segundo start pisa... no: se rechaza.
            if self._session is not None and not self._session.expired:
                return None
            self._session = session
            self._accept_until = 0.0        # la ventana se consume con el start

        return {
            "session_id": session.sid,
            "node_id": self.identity.node_id,
            "pubkey": self.identity.pubkey_hex,
            "cert_pem": self.identity.cert_pem,
            "name": self.node_name,
            "nonce": session.own_nonce,
        }

    def receive_reveal(self, sid: str, nonce_hex: str) -> bool:
        s = self._get_live(sid, role="receiver")
        if s is None or s.sas is not None:
            return False
        if blake3(bytes.fromhex(nonce_hex)).hexdigest() != s.peer_commit:
            s.state = "failed"              # commit roto: alguien intentó moler
            return False
        s.sas = compute_sas(s.peer_pubkey_hex, self.identity.pubkey_hex,
                            nonce_hex, s.own_nonce, self.identity.cert_pem)
        return True

    def receive_confirm(self, sid: str) -> str:
        """La confirmación del INICIADOR. 'confirmed' solo si el humano de este
        lado ya confirmó también; en ese momento se persiste el peer."""
        s = self._get_live(sid, role="receiver")
        if s is None or s.sas is None:
            return "unknown"
        if not s.local_confirmed:
            return "waiting"
        if s.state != "done":
            self._persist_peer(s)
            s.state = "done"
        return "confirmed"

    # --- iniciador --------------------------------------------------------------
    async def initiate(self, target: str) -> PairingSession:
        nonce = secrets.token_hex(32)
        async with self.client_factory(target) as client:
            r = await client.post("/p2p/v1/pair/start", json={
                "node_id": self.identity.node_id,
                "pubkey": self.identity.pubkey_hex,
                "cert_pem": self.identity.cert_pem,
                "name": self.node_name,
                "peer_port": self.peer_port,
                "commit": blake3(bytes.fromhex(nonce)).hexdigest(),
            })
            if r.status_code == 403:
                raise PairingRejected(
                    "el otro nodo no está aceptando pairings: ejecuta allí `tmisc pair --accept`")
            if r.status_code != 200:
                raise PairingRejected(f"start rechazado: HTTP {r.status_code}")
            data = r.json()

            if not _valid_announcement(data.get("node_id", ""), data.get("pubkey", ""),
                                       data.get("cert_pem", "")):
                raise PairingRejected("la identidad anunciada por el otro nodo no es coherente")

            r = await client.post("/p2p/v1/pair/reveal", json={
                "session_id": data["session_id"], "nonce": nonce,
            })
            if r.status_code != 200:
                raise PairingRejected(f"reveal rechazado: HTTP {r.status_code}")

        session = PairingSession(
            sid=data["session_id"],
            role="initiator",
            peer_node_id=data["node_id"],
            peer_pubkey_hex=data["pubkey"],
            peer_cert_pem=data["cert_pem"],
            peer_name=data.get("name", ""),
            peer_addr=target,
            own_nonce=nonce,
            sas=compute_sas(self.identity.pubkey_hex, data["pubkey"],
                            nonce, data["nonce"], data["cert_pem"]),
        )
        with self._lock:
            self._session = session
        return session

    async def push_confirm(self, sid: str) -> str:
        """Empuja nuestra confirmación al receptor. 'confirmed' cierra el pairing."""
        s = self._get_live(sid, role="initiator")
        if s is None:
            return "unknown"
        if not s.local_confirmed:
            return "waiting"
        if s.state == "done":
            return "confirmed"
        async with self.client_factory(s.peer_addr) as client:
            r = await client.post("/p2p/v1/pair/confirm", json={"session_id": sid})
        if r.status_code != 200:
            return "waiting"
        state = r.json().get("state", "waiting")
        if state == "confirmed":
            self._persist_peer(s)
            s.state = "done"
        return state

    # --- común -------------------------------------------------------------------
    def confirm_local(self, sid: str) -> bool:
        with self._lock:
            s = self._session
        if s is None or s.sid != sid or s.expired or s.sas is None:
            return False
        s.local_confirmed = True
        return True

    def current(self) -> PairingSession | None:
        with self._lock:
            return self._session

    def _get_live(self, sid: str, role: str) -> PairingSession | None:
        with self._lock:
            s = self._session
        if s is None or s.sid != sid or s.role != role or s.expired or s.state == "failed":
            return None
        return s

    def _persist_peer(self, s: PairingSession) -> None:
        self.peers.add(Peer(
            node_id=s.peer_node_id,
            pubkey_hex=s.peer_pubkey_hex,
            cert_pem=s.peer_cert_pem,
            name=s.peer_name,
            addr=s.peer_addr,
        ))


class PairingRejected(Exception):
    pass
