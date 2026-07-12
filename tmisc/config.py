"""Configuración del nodo. Todo por variables de entorno TMISC_* o flags de la CLI."""

import os
from dataclasses import dataclass

PROFILE_NODE = "node"      # LAN: sirve audio. La PC del usuario, la rocola.
PROFILE_TOOLS = "tools"    # solo encode/decode/probe. NINGUNA ruta de streaming existe.

DEFAULT_PORT = 8787

# 100 MB se quedaba corto para un FLAC largo.
DEFAULT_MAX_UPLOAD = 512 * 1024 * 1024


@dataclass
class Settings:
    library: str = "./library"
    cache_dir: str = "./cache"
    # Vacío = derivar de cache_dir. Solo se respeta tal cual si lo fija TMISC_DB,
    # para que cambiar --cache por la CLI mueva también la base de datos.
    db_path: str = ""
    # Bind a loopback por defecto: el gateway sirve la biblioteca musical entera,
    # y exponerla a toda la LAN debe ser una decisión explícita.
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    profile: str = PROFILE_NODE
    token: str | None = None
    max_upload: int = DEFAULT_MAX_UPLOAD
    public_base_url: str | None = None
    node_name: str = "tmisc-node"
    cover_side: int = 500
    cover_depth: int = 24
    cover_container: str = "bmp"

    # --- P2P LAN -------------------------------------------------------------
    p2p: bool = False
    # El canal entre nodos es OTRO listener (TLS, solo peers emparejados). El
    # player local sigue en `host:port` en HTTP plano.
    peer_host: str = "0.0.0.0"
    peer_port: int = 0            # 0 = port + 1
    # Vacío = <cache_dir>/node. Identidad y peers confiados: lo ÚNICO que NO es
    # derivable de los ficheros. Borrarlo re-clavea el nodo (re-pairing).
    state_dir: str = ""
    mdns: bool = True
    peer_connect_timeout: float = 2.0
    peer_read_timeout: float = 10.0
    # Al reproducir de un peer: proxy inmediato + descarga verificada al caché,
    # para que la segunda reproducción sea 100% local.
    fetch_through: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.library = os.getenv("TMISC_LIBRARY", s.library)
        s.cache_dir = os.getenv("TMISC_CACHE", s.cache_dir)
        s.db_path = os.getenv("TMISC_DB", "")
        s.host = os.getenv("TMISC_HOST", s.host)
        s.port = int(os.getenv("TMISC_PORT", s.port))
        s.profile = os.getenv("TMISC_PROFILE", s.profile)
        s.token = os.getenv("TMISC_TOKEN") or None
        s.max_upload = int(os.getenv("TMISC_MAX_UPLOAD", s.max_upload))
        s.public_base_url = os.getenv("TMISC_PUBLIC_BASE_URL") or None
        s.node_name = os.getenv("TMISC_NODE_NAME", s.node_name)
        s.p2p = os.getenv("TMISC_P2P", "").lower() in ("1", "true", "yes", "on")
        s.peer_host = os.getenv("TMISC_PEER_HOST", s.peer_host)
        s.peer_port = int(os.getenv("TMISC_PEER_PORT", s.peer_port))
        s.mdns = os.getenv("TMISC_MDNS", "1").lower() not in ("0", "false", "no", "off")
        return s.resolved()

    def resolved(self) -> "Settings":
        self.library = os.path.abspath(self.library)
        self.cache_dir = os.path.abspath(self.cache_dir)
        explicit_db = os.getenv("TMISC_DB")
        self.db_path = explicit_db or os.path.join(self.cache_dir, "catalog.db")
        # Igual que db_path: se re-deriva de cache_dir en cada resolved(), para
        # que un --cache tardío (CLI) también mueva el estado del nodo.
        explicit_state = os.getenv("TMISC_STATE_DIR")
        self.state_dir = os.path.abspath(explicit_state) if explicit_state \
            else os.path.join(self.cache_dir, "node")
        return self

    @property
    def serves_audio(self) -> bool:
        return self.profile == PROFILE_NODE

    @property
    def effective_peer_port(self) -> int:
        return self.peer_port or self.port + 1

    @property
    def fetched_dir(self) -> str:
        return os.path.join(self.cache_dir, "fetched")

    def ensure_dirs(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(os.path.join(self.cache_dir, "audio"), exist_ok=True)
        os.makedirs(os.path.join(self.cache_dir, "cover"), exist_ok=True)
        if self.serves_audio:
            os.makedirs(self.library, exist_ok=True)
            os.makedirs(self.fetched_dir, exist_ok=True)
