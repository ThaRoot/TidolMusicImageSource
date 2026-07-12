"""Factory de la app.

Los routers se registran CONDICIONALMENTE según el perfil. No es un `if` en
runtime: en el perfil `tools` las rutas de streaming literalmente no existen, así
que no hay forma de que un despliegue mal configurado acabe sirviendo audio.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tmisc.catalog import Catalog
from tmisc.config import PROFILE_NODE, Settings

from . import routes_catalog, routes_stream, routes_tools

VERSION = "2.0.0"


def create_app(settings: Settings | None = None, catalog: Catalog | None = None,
               identity=None, peers=None, sessions=None, network=None,
               pairing=None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    if catalog is None and settings.serves_audio:
        catalog = Catalog(settings.db_path)

    app = FastAPI(
        title="TMISC — contenedor de audio en imagen + gateway LAN",
        version=VERSION,
        description="Un .tmisc es un BMP válido que lleva dentro el audio, sus "
                    "metadatos y su hash. Este servicio lo sirve por HTTP con "
                    "Range, así que VLC, mpv o cualquier cliente lo reproducen "
                    "con seek sin saber nada del formato.",
    )

    # Sin CORS, el <audio crossOrigin="anonymous"> de TidolAudioEngine falla con
    # un error opaco. Y sin expose_headers, el JS no puede leer Content-Range.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
        allow_headers=["Range", "Authorization", "Content-Type"],
        expose_headers=[
            "Content-Range", "Accept-Ranges", "Content-Length",
            "X-Tmisc-Id", "X-Tmisc-Mode", "X-Tmisc-Overhead",
        ],
    )

    app.state.settings = settings
    app.state.catalog = catalog if settings.serves_audio else None
    app.state.identity = identity
    app.state.peers = peers
    app.state.sessions = sessions
    app.state.network = network
    app.state.pairing = pairing

    app.include_router(routes_tools.router)
    if settings.serves_audio:
        app.include_router(routes_stream.router)
        app.include_router(routes_catalog.router)
        if peers is not None:
            from . import routes_peers
            app.include_router(routes_peers.router)

    @app.get("/health")
    def health():
        tracks = catalog.count() if settings.serves_audio else None
        return {"status": "ok", "service": "tmisc", "version": VERSION,
                "profile": settings.profile, "tracks": tracks}

    @app.get("/")
    def root():
        endpoints = ["/encode", "/decode", "/probe", "/capacity", "/health"]
        if settings.serves_audio:
            endpoints = [
                "/stream/{id}", "/cover/{id}", "/track/{id}", "/file/{id}",
                "/catalog", "/playlist.xspf", "/playlist.m3u8",
                "/catalog/rescan", "/import", "/.well-known/tmisc.json",
            ] + endpoints
            if peers is not None:
                endpoints += ["/peers", "/network/catalog", "/fetch/{id}"]
        return JSONResponse({
            "service": "tmisc",
            "version": VERSION,
            "profile": settings.profile,
            "formato": ".tmisc v2 — BMP válido + audio + metadatos JSON + footer",
            "modo": "append (producción, ~1.0x) · lsb (legacy, 10.4x, sin seek)",
            "id": "BLAKE3 del audio: dos .tmisc con distinta portada son el mismo track",
            "endpoints": endpoints,
        })

    return app


def create_peer_app(settings: Settings, identity, peers, catalog: Catalog,
                    sessions=None, pairing=None) -> FastAPI:
    """El listener P2P: TLS con el cert del nodo, en OTRO puerto.

    Sin CORS (no lo usan navegadores), sin docs (menos superficie), sin el
    token del player. La única puerta es la sesión de peer emparejado.
    `sessions` se comparte con el app principal para que revocar un peer desde
    /peers invalide sus sesiones vivas aquí.
    """
    from tmisc.peers import SessionManager

    from . import routes_p2p

    app = FastAPI(title="TMISC P2P", version=VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.identity = identity
    app.state.peers = peers
    app.state.sessions = sessions if sessions is not None else SessionManager(identity, peers)
    app.state.pairing = pairing
    app.include_router(routes_p2p.router)

    @app.get("/p2p/v1/ping")
    def ping():
        return {"tmisc": 2, "node_id": identity.node_id, "proto": "p2p-v1"}

    return app
