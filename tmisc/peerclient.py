"""Cliente del canal peer<->peer.

Un `PeerClient` habla con UN peer emparejado: TLS pineado a su cert, handshake
de sesión automático (y re-handshake una sola vez si la sesión caduca a mitad).

El transporte es inyectable: los tests le pasan un `httpx.ASGITransport` con la
peer app del otro nodo y ejercitan el protocolo completo in-process, sin
sockets. En producción el transporte es el TLS pineado de verdad — que se
valida aparte, con un handshake real, en test_identity.py.
"""

import asyncio
import os
import time

import httpx

from .errors import TmiscError
from .hashing import hash_file_range
from .identity import NodeIdentity, pinned_ssl_context
from .peers import Peer, session_message

DOWNLOAD_CHUNK = 1 << 20


class PeerError(TmiscError):
    """El peer no responde o respondió algo imposible."""


class PeerNotPaired(PeerError):
    """El peer ya no nos reconoce (nos revocó, o se re-claveó)."""


class PeerClient:
    def __init__(self, identity: NodeIdentity, peer: Peer, *,
                 addr: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 connect_timeout: float = 2.0,
                 read_timeout: float = 10.0) -> None:
        self.identity = identity
        self.peer = peer
        self._session: str | None = None

        base_url = f"https://{addr or peer.addr}"
        kwargs: dict = {
            "base_url": base_url,
            "timeout": httpx.Timeout(read_timeout, connect=connect_timeout),
        }
        if transport is not None:
            kwargs["transport"] = transport      # tests: ASGI in-process
        else:
            kwargs["verify"] = pinned_ssl_context(peer.cert_pem)
        self._client = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- handshake -----------------------------------------------------------
    async def _handshake(self) -> None:
        try:
            r = await self._client.post("/p2p/v1/session/challenge",
                                        json={"node_id": self.identity.node_id})
        except httpx.HTTPError as e:
            raise PeerError(f"peer {self.peer.node_id[:12]} inalcanzable: {e}") from e
        if r.status_code == 403:
            raise PeerNotPaired(f"el peer {self.peer.node_id[:12]} ya no nos reconoce")
        if r.status_code != 200:
            raise PeerError(f"challenge inesperado: HTTP {r.status_code}")

        challenge = r.json()["challenge"]
        message = session_message(challenge, self.identity.node_id,
                                  self.peer.node_id, self.peer.cert_pem)
        r = await self._client.post("/p2p/v1/session", json={
            "node_id": self.identity.node_id,
            "challenge": challenge,
            "sig": self.identity.sign(message).hex(),
        })
        if r.status_code != 200:
            # Firma rechazada con el cert que tenemos pineado: o nos revocaron
            # o el peer se re-claveó. En ambos casos, toca re-pairing.
            raise PeerNotPaired(f"handshake rechazado por {self.peer.node_id[:12]}")
        self._session = r.json()["session"]

    async def _request(self, method: str, url: str, *,
                       headers: dict[str, str] | None = None,
                       stream: bool = False) -> httpx.Response:
        """Petición autenticada, con UN re-handshake si la sesión caducó."""
        for attempt in (1, 2):
            if self._session is None:
                await self._handshake()
            req_headers = {"Authorization": f"Bearer {self._session}", **(headers or {})}
            try:
                if stream:
                    req = self._client.build_request(method, url, headers=req_headers)
                    resp = await self._client.send(req, stream=True)
                else:
                    resp = await self._client.request(method, url, headers=req_headers)
            except httpx.HTTPError as e:
                raise PeerError(f"peer {self.peer.node_id[:12]} inalcanzable: {e}") from e
            if resp.status_code == 401 and attempt == 1:
                if stream:
                    await resp.aclose()
                self._session = None
                continue
            return resp
        raise PeerError("re-handshake agotado")     # inalcanzable

    # --- API -----------------------------------------------------------------
    async def track(self, track_id: str) -> dict | None:
        r = await self._request("GET", f"/p2p/v1/track/{track_id}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise PeerError(f"/track: HTTP {r.status_code}")
        return r.json()

    async def catalog(self, q: str | None = None, limit: int = 200) -> list[dict]:
        params = f"?limit={limit}" + (f"&q={httpx.QueryParams({'q': q})['q']}" if q else "")
        r = await self._request("GET", f"/p2p/v1/catalog{params}")
        if r.status_code != 200:
            raise PeerError(f"/catalog: HTTP {r.status_code}")
        return r.json()["items"]

    async def open_file_range(self, track_id: str, start: int,
                              end: int | None = None) -> httpx.Response:
        """GET /p2p/v1/file con Range, en streaming.

        Devuelve la respuesta ABIERTA (status 206/200): el llamante itera
        `resp.aiter_bytes()` y DEBE llamar a `resp.aclose()`.
        """
        rng = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
        resp = await self._request("GET", f"/p2p/v1/file/{track_id}",
                                   headers={"Range": rng}, stream=True)
        if resp.status_code not in (200, 206):
            await resp.aclose()
            raise PeerError(f"/file: HTTP {resp.status_code}")
        return resp

    async def download_file(self, track_id: str, dest_dir: str) -> tuple[str, dict]:
        """Descarga el .tmisc completo, VERIFICADO, a `dest_dir`.

        El id es el BLAKE3 del audio, así que la verificación es gratis: se
        hashea el slice [audio_offset, audio_offset+audio_size) del fichero
        descargado y se compara con el propio id. Si no cuadra, el .part se
        borra y no se indexa nada.
        """
        meta = await self.track(track_id)
        if meta is None:
            raise PeerError(f"el peer ya no tiene {track_id[:12]}")

        final = os.path.join(dest_dir, f"{track_id}.tmisc")
        part = final + ".part"
        resp = await self._request("GET", f"/p2p/v1/file/{track_id}", stream=True)
        try:
            if resp.status_code != 200:
                raise PeerError(f"/file: HTTP {resp.status_code}")
            with open(part, "wb") as fh:
                async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK):
                    fh.write(chunk)
        finally:
            await resp.aclose()

        try:
            if os.path.getsize(part) != meta["file_size"]:
                raise PeerError("descarga truncada")
            with open(part, "rb") as fh:
                got = hash_file_range(fh, meta["audio_offset"], meta["audio_size"]).hex()
            if got != track_id:
                raise PeerError(
                    f"el audio descargado de {self.peer.node_id[:12]} no coincide "
                    f"con su hash: esperado {track_id[:12]}…, llegó {got[:12]}…"
                )
        except PeerError:
            os.unlink(part)
            raise

        os.replace(part, final)
        return final, meta


RESOLVE_TTL = 30.0


class PeerNetwork:
    """Orquesta a todos los peers: resolución, agregación y fetch-through.

    Vive en el app PRINCIPAL. Los handlers /p2p/v1/* jamás lo tocan — por eso
    un ciclo de proxies A->B->A es imposible por construcción.
    """

    def __init__(self, identity: NodeIdentity, peers, catalog, settings, *,
                 transport_factory=None) -> None:
        self.identity = identity
        self.peers = peers
        self.catalog = catalog
        self.settings = settings
        # tests: peer -> httpx.ASGITransport hacia la peer app del otro nodo
        self.transport_factory = transport_factory
        self._clients: dict[str, PeerClient] = {}
        # caché de resolución, positiva Y negativa: una playlist de 50 tracks
        # ausentes no debe martillear a los peers 50 veces por segundo.
        self._resolve_cache: dict[str, tuple[float, tuple[str, dict] | None]] = {}
        self._fetching: set[str] = set()

    def client_for(self, peer: Peer) -> PeerClient:
        client = self._clients.get(peer.node_id)
        if client is None:
            transport = self.transport_factory(peer) if self.transport_factory else None
            client = PeerClient(
                self.identity, peer, transport=transport,
                connect_timeout=self.settings.peer_connect_timeout,
                read_timeout=self.settings.peer_read_timeout,
            )
            self._clients[peer.node_id] = client
        return client

    async def drop_client(self, node_id: str) -> None:
        client = self._clients.pop(node_id, None)
        if client is not None:
            await client.aclose()

    async def resolve_remote(self, track_id: str) -> tuple[Peer, dict] | None:
        """¿Qué peer tiene este track? Consulta paralela con caché de 30 s."""
        now = time.monotonic()
        hit = self._resolve_cache.get(track_id)
        if hit is not None and hit[0] > now:
            if hit[1] is None:
                return None
            peer = self.peers.get(hit[1][0])
            if peer is not None:
                return peer, hit[1][1]

        peers = self.peers.all()
        if peers:
            async def ask(peer: Peer):
                try:
                    return peer, await self.client_for(peer).track(track_id)
                except PeerError:
                    return peer, None       # peer caído o revocado: se ignora

            for peer, meta in await asyncio.gather(*(ask(p) for p in peers)):
                if meta is not None:
                    self._resolve_cache[track_id] = (now + RESOLVE_TTL,
                                                     (peer.node_id, meta))
                    return peer, meta

        self._resolve_cache[track_id] = (now + RESOLVE_TTL, None)
        return None

    async def read_remote_range(self, peer: Peer, track_id: str,
                                start: int, end: int) -> bytes:
        resp = await self.client_for(peer).open_file_range(track_id, start, end)
        try:
            return b"".join([chunk async for chunk in resp.aiter_bytes(DOWNLOAD_CHUNK)])
        finally:
            await resp.aclose()

    async def aggregate_catalog(self, q: str | None = None) -> list[tuple[Peer, list[dict]]]:
        peers = self.peers.all()
        if not peers:
            return []

        async def ask(peer: Peer):
            try:
                return peer, await self.client_for(peer).catalog(q)
            except PeerError:
                return peer, []

        return list(await asyncio.gather(*(ask(p) for p in peers)))

    # --- fetch-through ---------------------------------------------------------
    async def fetch_now(self, track_id: str, node_id: str) -> bool:
        """Trae el .tmisc del peer, verificado, y lo indexa. True si quedó local."""
        if self.catalog.resolve(track_id) is not None:
            return True
        if track_id in self._fetching:
            return False                     # single-flight: ya viene en camino
        peer = self.peers.get(node_id)
        if peer is None:
            return False

        self._fetching.add(track_id)
        try:
            path, _meta = await self.client_for(peer).download_file(
                track_id, self.settings.fetched_dir)
            from .scanner import index_file
            index_file(path, self.catalog)
            return True
        finally:
            self._fetching.discard(track_id)

    async def fetch_through(self, track_id: str, node_id: str) -> None:
        """La variante de fondo: cualquier fallo se queda en un log, no en VLC."""
        try:
            await self.fetch_now(track_id, node_id)
        except PeerError:
            pass

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
