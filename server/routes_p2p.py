"""Rutas del listener P2P (TLS, otro puerto). Solo hablan con peers emparejados.

Dos propiedades por construcción, no por disciplina:

  * Aquí no existe el token compartido del player: la única auth es la sesión
    obtenida con el challenge firmado (Ed25519). Un `?k=` filtrado en una URL
    jamás abre el canal entre nodos.
  * Aquí no existe el fallback a otros peers: /p2p/v1/* responde SOLO con el
    catálogo local y devuelve 404 si no lo tiene. Un ciclo A->B->A de proxies
    es imposible.
"""

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .deps import get_catalog, parse_ident
from .routes_stream import _info_from_row
from .streaming import file_slice, range_response

router = APIRouter(prefix="/p2p/v1")


async def require_peer_session(request: Request) -> str:
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    node_id = request.app.state.sessions.peer_for(token) if token else None
    if node_id is None:
        raise HTTPException(401, "Sesión de peer ausente o caducada.")
    request.state.peer_node_id = node_id
    return node_id


# --- handshake (estos dos endpoints SON la autenticación) ---------------------

@router.post("/session/challenge")
def session_challenge(payload: dict, request: Request):
    node_id = str(payload.get("node_id", ""))
    challenge = request.app.state.sessions.new_challenge(node_id)
    if challenge is None:
        raise HTTPException(403, "Peer no emparejado. Ejecuta `tmisc pair` primero.")
    return {"challenge": challenge, "node_id": request.app.state.identity.node_id}


@router.post("/session")
def session_redeem(payload: dict, request: Request):
    token = request.app.state.sessions.redeem(
        str(payload.get("node_id", "")),
        str(payload.get("challenge", "")),
        str(payload.get("sig", "")),
    )
    if token is None:
        raise HTTPException(401, "Challenge o firma inválidos.")
    return {"session": token,
            "expires_in": int(request.app.state.sessions.session_ttl)}


# --- pairing (gated por la ventana --accept, no por sesión) --------------------

@router.post("/pair/start")
def pair_start(payload: dict, request: Request):
    pairing = request.app.state.pairing
    if pairing is None or not pairing.accept_open():
        raise HTTPException(403, "Este nodo no está aceptando pairings ahora. "
                                 "Ejecuta `tmisc pair --accept` en él.")
    resp = pairing.receive_start(payload,
                                 request.client.host if request.client else "")
    if resp is None:
        raise HTTPException(400, "Identidad o commit inválidos.")
    return resp


@router.post("/pair/reveal")
def pair_reveal(payload: dict, request: Request):
    pairing = request.app.state.pairing
    if pairing is None:
        raise HTTPException(403, "P2P no activo.")
    try:
        ok = pairing.receive_reveal(str(payload.get("session_id", "")),
                                    str(payload.get("nonce", "")))
    except ValueError:
        ok = False
    if not ok:
        raise HTTPException(400, "Reveal inválido: el nonce no cuadra con el commit.")
    return {"ok": True}


@router.post("/pair/confirm")
def pair_confirm(payload: dict, request: Request):
    pairing = request.app.state.pairing
    if pairing is None:
        raise HTTPException(403, "P2P no activo.")
    state = pairing.receive_confirm(str(payload.get("session_id", "")))
    if state == "unknown":
        raise HTTPException(404, "Sesión de pairing desconocida o caducada.")
    return {"state": state}


# --- datos (siempre con sesión) ------------------------------------------------

@router.get("/catalog", dependencies=[Depends(require_peer_session)])
def p2p_catalog(request: Request, q: str | None = None,
                limit: int = 200, offset: int = 0):
    limit = max(1, min(limit, 1000))
    total, tracks = get_catalog(request).search(q=q, limit=limit, offset=offset)
    return JSONResponse({
        "node_id": request.app.state.identity.node_id,
        "total": total,
        "items": [t.as_dict() for t in tracks],
    })


@router.get("/track/{ident}", dependencies=[Depends(require_peer_session)])
def p2p_track(ident: str, request: Request):
    """La metadata que la pasarela necesita para servir sin tocar la red:
    offsets para trasladar Ranges, tamaños para HEAD/416, mime para VLC."""
    cat = get_catalog(request)
    row = cat.resolve(parse_ident(ident))
    if row is None or not os.path.exists(row["path"]):
        raise HTTPException(404, "Este nodo no tiene el track.")

    # El id que devolvemos es el canónico, no el que haya pedido el peer: la
    # pasarela del otro lado lo usa para verificar el BLAKE3 de lo que descargue.
    track_id = row["id"]
    raw = cat.get_meta_json(track_id)
    return JSONResponse({
        "id": track_id,
        "file_size": row["file_size"],
        "image_size": row["image_size"],
        "audio_offset": row["audio_offset"],
        "audio_size": row["audio_size"],
        "mime": row["mime"],
        "ext": row["ext"],
        "streamable": row["streamable"],
        "container_ver": row["container_ver"],
        "mtime_ns": row["mtime_ns"],
        "file_b3": row["file_b3"],
        "mode": row["mode"],
        "meta": json.loads(raw) if raw else None,
    })


@router.api_route("/file/{ident}", methods=["GET", "HEAD"],
                  dependencies=[Depends(require_peer_session)])
def p2p_file(ident: str, request: Request):
    """El .tmisc entero con Range: el único endpoint de datos del canal P2P.

    Sirve para el streaming proxied (la pasarela traslada el rango del audio a
    [audio_offset+s, audio_offset+e]) y para el fetch-through al caché.
    """
    row = get_catalog(request).resolve(parse_ident(ident))
    if row is None or not os.path.exists(row["path"]):
        raise HTTPException(404, "Este nodo no tiene el track.")
    track_id = row["id"]
    info = _info_from_row(row)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "ETag": f'"f-{track_id[:32]}-{row["file_size"]:x}-{row["mtime_ns"]:x}"',
        "Cache-Control": "public, max-age=86400",
        "X-Tmisc-Id": track_id,
    }
    return range_response(
        request, info.file_size, headers,
        lambda start, length: file_slice(info.path, start, length),
    )
