"""Estado compartido del servidor: settings, catálogo y auth opcional.

El estado vive en `app.state`, no en globals de módulo: dos nodos (dos
`create_app` con settings distintos) tienen que poder convivir en el mismo
proceso. Es lo que hacen los tests de P2P, que levantan un nodo A y un nodo B
in-process y los conectan entre sí.
"""

import ipaddress
import re

from fastapi import HTTPException, Request

from tmisc.catalog import Catalog
from tmisc.config import Settings

# Un id es siempre un hash hexadecimal, jamás una ruta. La resolución a fichero
# pasa SIEMPRE por SQLite, así que no hay superficie de path traversal.
IDENT_RE = re.compile(r"^([0-9a-f]{8,64})(?:\.([a-z0-9]{1,8}))?$")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_catalog(request: Request) -> Catalog:
    catalog = request.app.state.catalog
    if catalog is None:
        raise HTTPException(503, "Este nodo no tiene catálogo (perfil 'tools').")
    return catalog


def parse_ident(ident: str) -> str:
    m = IDENT_RE.match(ident)
    if not m:
        raise HTTPException(400, "Identificador inválido.")
    return m.group(1)


def base_url(request: Request) -> str:
    """URL absoluta del nodo, vista desde quien pregunta.

    Devolver rutas relativas o 'localhost' haría que una playlist abierta en otra
    máquina apuntase al sitio equivocado. Es un fallo silencioso muy fácil.
    """
    s = get_settings(request)
    if s.public_base_url:
        return s.public_base_url.rstrip("/")
    host = request.headers.get("host") or f"{s.host}:{s.port}"
    return f"{request.url.scheme}://{host}"


async def require_token(request: Request) -> None:
    """Token opcional. Se acepta por query param además de por cabecera.

    VLC no manda cabeceras propias con `vlc <url>`, así que `?k=` es la única vía
    práctica para él.
    """
    s = get_settings(request)
    if not s.token:
        return
    if request.method == "OPTIONS":
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("k")
    if supplied != s.token:
        raise HTTPException(401, "Token inválido o ausente.")


async def require_loopback(request: Request) -> None:
    """Mutaciones del anillo de confianza (pairing, alta/baja de peers): solo
    desde la propia máquina. Exponer el player a la LAN con --host 0.0.0.0 no
    debe exponer también el control de en quién confía este nodo.
    """
    client = request.client
    try:
        is_loopback = client is not None and ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(403, "Solo accesible desde la propia máquina.")
