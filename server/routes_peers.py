"""Administración del anillo de confianza — vive en el app PRINCIPAL.

Listar peers es inofensivo (va con el token del player, como el catálogo).
MUTAR el anillo (borrar peers; en fases siguientes, emparejar) exige además
origen loopback: exponer el player a la LAN con --host 0.0.0.0 no debe exponer
el control de en quién confía este nodo.
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .deps import base_url, get_catalog, parse_ident, require_loopback, require_token
from .routes_catalog import _jobs, _jobs_lock

router = APIRouter(dependencies=[Depends(require_token)])


def _network(request: Request):
    network = request.app.state.network
    if network is None:
        raise HTTPException(503, "P2P no activo. Arranca con `tmisc serve --p2p`.")
    return network


@router.get("/peers")
def peers_list(request: Request):
    peers = request.app.state.peers
    return JSONResponse({
        "node_id": request.app.state.identity.node_id,
        "peers": [
            {"node_id": p.node_id, "name": p.name, "addr": p.addr,
             "added_at": p.added_at}
            for p in peers.all()
        ],
    })


@router.delete("/peers/{needle}", dependencies=[Depends(require_loopback)])
async def peers_remove(needle: str, request: Request):
    """Revocación unilateral: fuera del store y sesiones invalidadas al instante."""
    store = request.app.state.peers
    peer = store.match(needle)
    if peer is None:
        raise HTTPException(404, "Ningún peer coincide con ese id o nombre.")
    store.remove(peer.node_id)
    sessions = request.app.state.sessions
    if sessions is not None:
        sessions.invalidate_peer(peer.node_id)
    network = request.app.state.network
    if network is not None:
        await network.drop_client(peer.node_id)
    return {"removed": peer.node_id, "name": peer.name}


# --- la biblioteca de la red ----------------------------------------------------

@router.get("/network/catalog")
async def network_catalog(request: Request, q: str | None = None, limit: int = 500):
    """Catálogo agregado: lo local + lo de cada peer vivo, deduplicado por id.

    Las URLs siempre apuntan a ESTE nodo (la pasarela), tenga los bytes quien
    los tenga. Es la vista que un cliente (VLC, el frontend) debería usar.
    """
    from tmisc.playlists import cover_url, stream_url

    network = _network(request)
    base = base_url(request)
    limit = max(1, min(limit, 1000))

    _, local = get_catalog(request).search(q=q, limit=limit)
    items: dict[str, dict] = {
        t.id: {**t.as_dict(), "source": "local",
               "stream_url": stream_url(base, t), "cover_url": cover_url(base, t)}
        for t in local
    }
    for peer, remote_items in await network.aggregate_catalog(q):
        for it in remote_items:
            if it["id"] in items:
                continue                     # dedupe por content-address: local gana
            items[it["id"]] = {
                **it, "source": peer.node_id, "source_name": peer.name,
                "stream_url": f"{base}/stream/{it['id']}.{it['ext']}",
                "cover_url": f"{base}/cover/{it['id']}.jpg",
            }
    return JSONResponse({"total": len(items), "items": list(items.values())})


@router.post("/fetch/{ident}", status_code=202)
async def fetch_track(ident: str, request: Request, background: BackgroundTasks):
    """Trae un track de quien lo tenga, verificado, al caché local (job)."""
    network = _network(request)
    track_id = parse_ident(ident)
    if get_catalog(request).resolve(track_id) is not None:
        return {"state": "already-local", "id": track_id}

    found = await network.resolve_remote(track_id)
    if found is None:
        raise HTTPException(404, "Ningún peer tiene ese track.")
    peer, _meta = found

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "stats": {"id": track_id,
                                                       "from": peer.node_id[:16]}}

    async def run() -> None:
        try:
            ok = await network.fetch_now(track_id, peer.node_id)
            with _jobs_lock:
                _jobs[job_id] = {"state": "done" if ok else "error",
                                 "stats": {"id": track_id}}
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id] = {"state": "error", "error": str(e)}

    background.add_task(run)
    return {"job_id": job_id, "state": "running"}


# --- pairing: la UI local del protocolo (la ejecuta el daemon, la muestra la CLI)

def _pairing(request: Request):
    pairing = request.app.state.pairing
    if pairing is None:
        raise HTTPException(503, "P2P no activo. Arranca con `tmisc serve --p2p`.")
    return pairing


@router.post("/peers/pair/accept", dependencies=[Depends(require_loopback)])
def pair_accept(request: Request):
    """Abre la ventana de 120 s en la que este nodo acepta UN intento de pairing."""
    _pairing(request).open_accept()
    return {"accepting": True}


@router.get("/peers/pair/incoming", dependencies=[Depends(require_loopback)])
def pair_incoming(request: Request):
    s = _pairing(request).current()
    if s is None or s.role != "receiver":
        return JSONResponse({"session": None})
    return JSONResponse({"session": s.public_view()})


@router.post("/peers/pair", dependencies=[Depends(require_loopback)])
async def pair_initiate(payload: dict, request: Request):
    from tmisc.pairing import PairingRejected

    target = str(payload.get("target", "")).strip()
    if not target:
        raise HTTPException(400, "Falta 'target' (host:puerto_p2p del otro nodo).")
    if ":" not in target:
        # nombre mDNS o prefijo de node_id descubierto en la LAN
        discovery = getattr(request.app.state, "discovery", None)
        found = discovery.found if discovery is not None else {}
        for nid, entry in found.items():
            if entry["name"] == target or nid.startswith(target):
                target = f"{entry['host']}:{entry['port']}"
                break
        else:
            raise HTTPException(404, f"'{target}' no aparece por mDNS; "
                                     "usa host:puerto directamente.")
    try:
        session = await _pairing(request).initiate(target)
    except PairingRejected as e:
        raise HTTPException(502, str(e))
    return session.public_view()


@router.get("/peers/pair/{sid}", dependencies=[Depends(require_loopback)])
async def pair_status(sid: str, request: Request):
    pairing = _pairing(request)
    s = pairing.current()
    if s is None or s.sid != sid:
        raise HTTPException(404, "Sesión de pairing desconocida.")
    # El poll de la CLI empuja nuestra confirmación pendiente hacia el receptor:
    # el estado avanza sin tareas de fondo.
    if s.role == "initiator" and s.local_confirmed and s.state != "done":
        await pairing.push_confirm(sid)
    return s.public_view()


@router.post("/peers/pair/{sid}/confirm", dependencies=[Depends(require_loopback)])
async def pair_confirm_local(sid: str, request: Request):
    pairing = _pairing(request)
    if not pairing.confirm_local(sid):
        raise HTTPException(409, "No hay una sesión viva con ese id (¿caducó?).")
    s = pairing.current()
    if s.role == "initiator":
        await pairing.push_confirm(sid)
    return s.public_view()
