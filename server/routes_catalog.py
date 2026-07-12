"""/catalog, /playlist.*, /import, /jobs — solo en el perfil `node`."""

import threading
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from tmisc.playlists import cover_url, stream_url, to_m3u, to_xspf
from tmisc.scanner import ScanStats, scan

from .deps import base_url, get_catalog, get_settings, require_token

router = APIRouter(dependencies=[Depends(require_token)])

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


@router.get("/catalog")
def catalog(request: Request, q: str | None = None, artist: str | None = None,
            album: str | None = None, sort: str = "artist",
            limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 1000))
    total, tracks = get_catalog(request).search(q, artist, album, sort, limit, offset)
    base = base_url(request)
    return JSONResponse({
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {**t.as_dict(),
             "stream_url": stream_url(base, t),
             "cover_url": cover_url(base, t)}
            for t in tracks
        ],
    })


@router.get("/catalog/artists")
def artists(request: Request):
    return JSONResponse(get_catalog(request).facet("artist"))


@router.get("/catalog/albums")
def albums(request: Request):
    return JSONResponse(get_catalog(request).facet("album"))


@router.post("/catalog/rescan", status_code=202)
def rescan(request: Request, background: BackgroundTasks):
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "stats": ScanStats().as_dict()}

    settings, cat = get_settings(request), get_catalog(request)

    def run() -> None:
        def progress(st: ScanStats) -> None:
            with _jobs_lock:
                _jobs[job_id]["stats"] = st.as_dict()
        try:
            stats = scan(settings.library, cat, progress)
            with _jobs_lock:
                _jobs[job_id] = {"state": "done", "stats": stats.as_dict()}
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id] = {"state": "error", "error": str(e)}

    background.add_task(run)
    return {"job_id": job_id, "state": "running"}


@router.get("/jobs/{job_id}")
def job(job_id: str):
    with _jobs_lock:
        j = _jobs.get(job_id)
    if j is None:
        raise HTTPException(404, "Job desconocido.")
    return JSONResponse(j)


def _playlist_tracks(cat, q, artist, album, ids, limit):
    if ids:
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        _, found = cat.search(limit=10000)
        by_id = {t.id: t for t in found}
        return [by_id[i] for i in wanted if i in by_id]
    _, tracks = cat.search(q=q, artist=artist, album=album, limit=limit)
    return tracks


@router.get("/playlist.m3u8")
def playlist_m3u(request: Request, q: str | None = None, artist: str | None = None,
                 album: str | None = None, ids: str | None = None, limit: int = 500):
    tracks = _playlist_tracks(get_catalog(request), q, artist, album, ids, limit)
    body = to_m3u(tracks, base_url(request), title=q or album or artist or "Tidol")
    return PlainTextResponse(body, media_type="audio/x-mpegurl")


@router.get("/playlist.xspf")
def playlist_xspf(request: Request, q: str | None = None, artist: str | None = None,
                  album: str | None = None, ids: str | None = None, limit: int = 500):
    tracks = _playlist_tracks(get_catalog(request), q, artist, album, ids, limit)
    body = to_xspf(tracks, base_url(request), title=q or album or artist or "Tidol")
    return Response(body, media_type="application/xspf+xml")


@router.post("/import", status_code=202)
def import_path(request: Request, payload: dict, background: BackgroundTasks):
    """Convierte audio suelto ya presente en disco a .tmisc. No descarga nada.

    Mismo contrato que `TidolCore::import_path(path) -> ImportSummary`, que el
    workspace de Tidol ya tenía documentado como placeholder.
    """
    from tmisc.importer import import_tree

    src = payload.get("path")
    if not src:
        raise HTTPException(400, "Falta 'path'.")

    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"state": "running", "stats": {}}

    settings, cat = get_settings(request), get_catalog(request)
    recursive = bool(payload.get("recursive", True))

    def run() -> None:
        try:
            summary = import_tree(src, settings, recursive=recursive)
            scan(settings.library, cat)
            with _jobs_lock:
                _jobs[job_id] = {"state": "done", "stats": summary}
        except Exception as e:
            with _jobs_lock:
                _jobs[job_id] = {"state": "error", "error": str(e)}

    background.add_task(run)
    return {"job_id": job_id, "state": "running"}


@router.get("/.well-known/tmisc.json")
def descriptor(request: Request):
    """Lo que un peer (o el frontend de Tidol) sondea para saber si hay un nodo aquí."""
    s = get_settings(request)
    base = base_url(request)
    payload = {
        "tmisc": 2,
        "node_name": s.node_name,
        "base_url": base,
        "tracks": get_catalog(request).count(),
        "endpoints": {
            "catalog": f"{base}/catalog",
            "stream": f"{base}/stream/{{id}}",
            "cover": f"{base}/cover/{{id}}",
            "file": f"{base}/file/{{id}}",
            "playlist": f"{base}/playlist.xspf",
        },
        "capabilities": ["range", "xspf", "m3u", "content-addressed"],
    }
    # La identidad es pública a propósito: la clave PÚBLICA no autoriza nada
    # (autorizar exige el pairing), pero permite a un peer saber a quién habla.
    ident = getattr(request.app.state, "identity", None)
    if ident is not None:
        payload["capabilities"].append("p2p-v1")
        payload["node"] = {
            "node_id": ident.node_id,
            "pubkey": ident.pubkey_hex,
            "peer_port": s.effective_peer_port,
        }
    return JSONResponse(payload)
