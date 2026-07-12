"""Descubrimiento de nodos en la LAN: mDNS, servicio `_tmisc._tcp.local.`

Cada nodo anuncia su listener P2P con TXT records (node_id, nombre, protocolo)
y a la vez observa la red. Cuando aparece un peer YA emparejado con otra IP,
su dirección se refresca en peers.json: las IPs de una LAN doméstica cambian,
la identidad Ed25519 no.

Esto es azúcar, nunca requisito: en Docker (bridge), WSL2 o Wi-Fi con AP
isolation el multicast no cruza. El pairing acepta `host:puerto` explícito y
`tmisc peers addr` corrige direcciones a mano.

El announce y el browser viven en el event loop del daemon (AsyncZeroconf),
no en threads sueltos: el shutdown es limpio y no hay estado compartido raro.
"""

import asyncio
import socket

from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

SERVICE = "_tmisc._tcp.local."
PROTO = "p2p-v1"


def primary_ip() -> str:
    """La IP de salida de esta máquina, sin mandar un solo paquete."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Discovery:
    def __init__(self, identity, settings, peers=None, network=None,
                 announce: bool = True) -> None:
        self.identity = identity
        self.settings = settings
        self.peers = peers
        self.network = network
        self.announce = announce
        self.found: dict[str, dict] = {}     # node_id -> {name, host, port}
        self._names: dict[str, str] = {}     # nombre mDNS -> node_id
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._info: ServiceInfo | None = None

    # --- estado puro (testeable sin multicast) --------------------------------
    def record(self, service_name: str, node_id: str, display_name: str,
               host: str, port: int) -> None:
        if node_id == self.identity.node_id:
            return                            # nuestro propio eco
        self.found[node_id] = {"name": display_name, "host": host, "port": port}
        self._names[service_name] = node_id

        # peer emparejado con dirección nueva: refrescar y tirar el cliente viejo
        addr = f"{host}:{port}"
        if self.peers is not None:
            peer = self.peers.get(node_id)
            if peer is not None and peer.addr != addr:
                self.peers.update_addr(node_id, addr)
                if self.network is not None:
                    try:
                        asyncio.get_running_loop().create_task(
                            self.network.drop_client(node_id))
                    except RuntimeError:
                        pass                  # fuera del loop (CLI discover): da igual

    def forget(self, service_name: str) -> None:
        node_id = self._names.pop(service_name, None)
        if node_id is not None:
            self.found.pop(node_id, None)

    def build_service_info(self) -> ServiceInfo:
        name = f"{self.settings.node_name}-{self.identity.node_id[:8]}.{SERVICE}"
        return ServiceInfo(
            SERVICE,
            name=name,
            port=self.settings.effective_peer_port,
            addresses=[socket.inet_aton(primary_ip())],
            properties={
                "id": self.identity.node_id,
                "name": self.settings.node_name,
                "proto": PROTO,
                "v": "2",
            },
        )

    # --- plomería zeroconf ------------------------------------------------------
    async def start(self) -> None:
        self._azc = AsyncZeroconf()
        if self.announce:
            self._info = self.build_service_info()
            await self._azc.async_register_service(self._info)
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, SERVICE, handlers=[self._on_change])

    def _on_change(self, zeroconf, service_type: str, name: str,
                   state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            self.forget(name)
            return
        asyncio.ensure_future(self._resolve(service_type, name))

    async def _resolve(self, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self._azc.zeroconf, timeout=3000):
            return
        props = {k.decode(): v.decode() if isinstance(v, bytes) else v
                 for k, v in (info.properties or {}).items() if v is not None}
        node_id = props.get("id", "")
        if len(node_id) != 64 or props.get("proto") != PROTO:
            return
        addresses = info.parsed_addresses()
        if not addresses or not info.port:
            return
        self.record(name, node_id, props.get("name", ""), addresses[0], info.port)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.async_cancel()
        if self._azc is not None:
            if self._info is not None:
                await self._azc.async_unregister_service(self._info)
            await self._azc.async_close()


async def browse(identity, settings, seconds: float = 3.0) -> dict[str, dict]:
    """Solo mirar (sin anunciarse): lo que usa `tmisc peers discover`."""
    d = Discovery(identity, settings, announce=False)
    await d.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await d.stop()
    return d.found
