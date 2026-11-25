"""/stream, /cover, /file, /track — solo en el perfil `node`.

Aquí es donde el formato se convierte en algo que VLC entiende. El cliente NUNCA
ve el contenedor: pide bytes del audio y recibe bytes del audio. El
`Content-Range` que devolvemos habla del tamaño del AUDIO, no del .tmisc.
Traducir un rango a un slice es `seek(audio_offset + start)`, O(1).

Tres trampas que son la diferencia entre que funcione y que no:

  HEAD   Starlette NO suprime el cuerpo automáticamente: con un StreamingResponse
         y methods=["GET","HEAD"], un HEAD envía el audio entero. Y VLC y mpv
         hacen HEAD para sondear si el servidor admite seek.

  CORS   un reproductor web con crossOrigin='anonymous' en sus <audio>, así que
         sin Access-Control-Allow-Origin el elemento falla con un error opaco, y
         sin expose_headers el JS no puede leer Content-Range.

  MIME   El Content-Type sale de los magic bytes del audio. Sin él, VLC va a
         ciegas y el <audio> se niega a sonar.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from tmisc.container import CHUNK, TmiscInfo, iter_audio_slice
from tmisc.format import MODE_PLAIN
from tmisc.sniff import mime_for_ext

from .deps import base_url, get_catalog, get_settings, parse_ident, require_token
from .streaming import file_slice, range_response

router = APIRouter(dependencies=[Depends(require_token)])


def _info_from_row(row) -> TmiscInfo:
    """Reconstruye el lector desde SQLite, sin volver a tocar el fichero."""
    return TmiscInfo(
        path=row["path"], file_size=row["file_size"], version=row["container_ver"],
        mode=row["mode"], audio_offset=row["audio_offset"], audio_size=row["audio_size"],
        image_size=row["image_size"], ext=row["ext"], mime=row["mime"],
    )


def _resolve(catalog, ident: str) -> tuple[str, TmiscInfo]:
    """(id canónico, info). Acepta prefijos: ver Catalog.expand_id."""
    row = catalog.resolve(ident)
    if row is None:
        raise HTTPException(404, "Track no encontrado en el catálogo.")
    if not os.path.exists(row["path"]):
        raise HTTPException(410, "El fichero ya no está en disco; reindexa el catálogo.")
    return row["id"], _info_from_row(row)


def _materialize_lsb(settings, info: TmiscInfo, track_id: str) -> TmiscInfo:
    """Un LSB no es un slice: hay que decodificar los píxeles una vez y cachear.

    A partir de ahí se sirve del fichero cacheado, ya con seek normal. En el
    catálogo aparece como streamable='cached' para que el cliente sepa que el
    primer play tiene latencia.
    """
    from tmisc.legacy_lsb import extract_lsb_audio

    cached = os.path.join(settings.cache_dir, "audio", f"{track_id}.{info.ext}")

    if not os.path.exists(cached):
        audio = extract_lsb_audio(info)
        tmp = cached + ".part"
        with open(tmp, "wb") as fh:
            fh.write(audio)
        os.replace(tmp, cached)

    size = os.path.getsize(cached)
    return TmiscInfo(
        path=cached, file_size=size, version=info.version,
        mode=3,                      # ya es un slice contiguo del fichero de caché
        audio_offset=0, audio_size=size, image_size=0,
        ext=info.ext, mime=info.mime,
    )


async def _resolve_or_remote(request: Request, ident: str):
    """Camino local o, si este nodo no tiene el track, la metadata de un peer.

    Devuelve (id_canónico, info_local, None) o (id_canónico, None, (peer, meta)).
    404 si nadie lo tiene — incluida la consulta a los peers, así que un id
    inexistente cuesta como mucho un round-trip por peer cada 30 s (caché
    negativa).

    El id canónico es SIEMPRE el de 64 hex, nunca el prefijo que haya pedido el
    cliente: de él cuelgan el ETag, el nombre en el caché y la verificación del
    BLAKE3 al traer el fichero del peer. Con un prefijo, esa verificación
    fallaría siempre.
    """
    catalog = get_catalog(request)
    row = catalog.resolve(ident)
    if row is not None:
        if not os.path.exists(row["path"]):
            raise HTTPException(410, "El fichero ya no está en disco; reindexa el catálogo.")
        return row["id"], _info_from_row(row), None

    network = request.app.state.network
    if network is not None:
        found = await network.resolve_remote(ident)
        if found is not None:
            # El peer expande el prefijo contra SU catálogo y devuelve el id entero.
            return found[1]["id"], None, found
    raise HTTPException(404, "Track no encontrado en el catálogo.")


def _pipe_remote(network, peer, track_id: str, abs_start: int, abs_end: int):
    """Async generator: re-emite el slice remoto y SIEMPRE cierra el upstream."""
    async def gen():
        upstream = await network.client_for(peer).open_file_range(
            track_id, abs_start, abs_end)
        try:
            async for chunk in upstream.aiter_bytes(CHUNK):
                yield chunk
        finally:
            await upstream.aclose()
    return gen()


def _etag(track_id: str, path: str, prefix: str = "") -> str:
    try:
        st = os.stat(path)
        return f'"{prefix}{track_id[:32]}-{st.st_size:x}-{st.st_mtime_ns:x}"'
    except OSError:
        return f'"{prefix}{track_id}"'


@router.api_route("/stream/{ident}", methods=["GET", "HEAD"])
async def stream(ident: str, request: Request):
    track_id, info, remote = await _resolve_or_remote(request, parse_ident(ident))

    if remote is not None:
        return await _stream_remote(request, track_id, remote)

    if not info.streamable:
        info = _materialize_lsb(get_settings(request), info, track_id)

    total = info.audio_size
    mime = info.mime or mime_for_ext(info.ext)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        # El id identifica el AUDIO, no los bytes que se sirven: editar los tags
        # cambia el fichero sin cambiar el id. El ETag va ligado al fichero para
        # que un Range reanudado (If-Range) nunca mezcle dos versiones.
        "ETag": _etag(track_id, info.path),
        "Cache-Control": "public, max-age=86400",
        # inline, no attachment: que el navegador reproduzca en vez de descargar.
        "Content-Disposition": f'inline; filename="{track_id[:16]}.{info.ext}"',
        "X-Tmisc-Id": track_id,
    }

    return range_response(
        request, total, headers,
        lambda start, length: iter_audio_slice(info, start, length, chunk=CHUNK),
    )


async def _stream_remote(request: Request, track_id: str, found):
    """La pasarela: VLC habla con ESTE nodo y este nodo trae el audio del peer.

    El Range del audio se traslada al Range del fichero remoto con una suma
    (audio_offset). HEAD y 416 se responden aquí, sin tocar la red. Tras servir
    la respuesta, el fetch-through descarga el .tmisc completo verificado al
    caché: la siguiente reproducción es 100% local.
    """
    peer, meta = found
    network = request.app.state.network
    settings = get_settings(request)

    if meta.get("streamable") != "direct":
        # LSB remoto: el audio no es un slice del fichero. Se trae entero, se
        # indexa, y el camino local (que sabe materializar LSB) lo sirve.
        if not await network.fetch_now(track_id, peer.node_id):
            raise HTTPException(503, "El track está en un peer pero aún no se pudo traer.")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(str(request.url), status_code=307)

    total = meta["audio_size"]
    audio_offset = meta["audio_offset"]
    mime = meta["mime"] or mime_for_ext(meta["ext"])
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        # content-address: da igual de qué nodo salgan los bytes
        # Ligado a la versión del fichero del peer (ver _etag): el id solo no basta.
        "ETag": f'"{track_id[:32]}-{meta["file_size"]:x}-{meta.get("mtime_ns", 0):x}"',
        "Cache-Control": "public, max-age=86400",
        "Content-Disposition": f'inline; filename="{track_id[:16]}.{meta["ext"]}"',
        "X-Tmisc-Id": track_id,
        "X-Tmisc-Source": peer.node_id[:16],
    }

    response = range_response(
        request, total, headers,
        lambda start, length: _pipe_remote(
            network, peer, track_id,
            audio_offset + start, audio_offset + start + length - 1),
    )
    if settings.fetch_through and request.method == "GET" and response.status_code != 416:
        response.background = BackgroundTask(
            network.fetch_through, track_id, peer.node_id)
    return response


@router.api_route("/cover/{ident}", methods=["GET", "HEAD"])
async def cover(ident: str, request: Request, size: int | None = None):
    wants_jpeg = ident.endswith((".jpg", ".jpeg")) or size is not None
    track_id, info, remote = await _resolve_or_remote(request, parse_ident(ident))

    if remote is not None:
        return await _cover_remote(request, track_id, remote, wants_jpeg, size)

    if info.mode == MODE_PLAIN:
        return _cover_plain(request, track_id, info, wants_jpeg, size)

    if not wants_jpeg:
        # El slice [0, image_size) es la imagen tal cual. Sin abrir Pillow.
        if not info.image_size:
            raise HTTPException(404, "Este .tmisc no tiene portada.")
        with open(info.path, "rb") as fh:
            data = fh.read(info.image_size)
        return Response(
            content=b"" if request.method == "HEAD" else data,
            media_type=info.image_mime,
            headers={"Content-Length": str(len(data)),
                     "Cache-Control": "public, max-age=86400"},
        )

    # Variante JPEG: ~30x más pequeña que el BMP crudo. Se cachea en disco.
    settings = get_settings(request)
    side = max(32, min(size or 300, 1000))
    cached = os.path.join(settings.cache_dir, "cover", f"{track_id}_{side}.jpg")

    if not os.path.exists(cached):
        from tmisc.cover import _fit_square, load_cover
        with open(info.path, "rb") as fh:
            img = load_cover(fh.read(info.image_size or info.audio_offset))
        _fit_square(img.convert("RGB"), side).save(cached, format="JPEG", quality=85, optimize=True)

    with open(cached, "rb") as fh:
        data = fh.read()
    return Response(
        content=b"" if request.method == "HEAD" else data,
        media_type="image/jpeg",
        headers={"Content-Length": str(len(data)),
                 "Cache-Control": "public, max-age=86400"},
    )


def _cover_plain(request: Request, track_id: str, info: TmiscInfo,
                 wants_jpeg: bool, size: int | None):
    """Portada de un fichero de audio normal: la embebida, o cover.jpg/folder.jpg.

    A diferencia de un .tmisc, la portada puede cambiar sin que cambie el id
    (editar la carátula en Picard no toca el audio): la caché va ligada al mtime
    y no se marca como inmutable.
    """
    from tmisc.importer import find_embedded_cover, find_folder_cover

    try:
        mtime = os.stat(info.path).st_mtime_ns
    except OSError:
        raise HTTPException(410, "El fichero ya no está en disco; reindexa el catálogo.")
    headers = {"Cache-Control": "public, max-age=3600", "ETag": f'"c-{track_id[:16]}-{mtime}"'}

    side = max(32, min(size or 300, 1000))
    cached = os.path.join(get_settings(request).cache_dir, "cover",
                          f"{track_id}_{side}_{mtime}.jpg")
    if wants_jpeg and os.path.exists(cached):
        with open(cached, "rb") as fh:
            data = fh.read()
        mime = "image/jpeg"
    else:
        data = find_embedded_cover(info.path) or find_folder_cover(info.path)
        if not data:
            raise HTTPException(404, "Este fichero no tiene portada.")
        mime = _image_mime(data[:4])
        if wants_jpeg:
            from tmisc.cover import _fit_square, load_cover
            _fit_square(load_cover(data).convert("RGB"), side).save(
                cached, format="JPEG", quality=85, optimize=True)
            with open(cached, "rb") as fh:
                data = fh.read()
            mime = "image/jpeg"

    headers["Content-Length"] = str(len(data))
    return Response(content=b"" if request.method == "HEAD" else data,
                    media_type=mime, headers=headers)


def _image_mime(head: bytes) -> str:
    if head[:2] == b"BM":
        return "image/bmp"
    if head[:4] == b"\x89PNG":
        return "image/png"
    if head[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "application/octet-stream"


async def _cover_remote(request: Request, track_id: str, found,
                        wants_jpeg: bool, size: int | None):
    """La portada remota es el slice [0, image_size) del fichero del peer."""
    peer, meta = found
    if not meta.get("image_size"):
        raise HTTPException(404, "Este .tmisc no tiene portada.")
    network = request.app.state.network
    data = await network.read_remote_range(peer, track_id, 0, meta["image_size"] - 1)

    if wants_jpeg:
        settings = get_settings(request)
        side = max(32, min(size or 300, 1000))
        cached = os.path.join(settings.cache_dir, "cover", f"{track_id}_{side}.jpg")
        if not os.path.exists(cached):
            from tmisc.cover import _fit_square, load_cover
            img = load_cover(data)
            _fit_square(img.convert("RGB"), side).save(
                cached, format="JPEG", quality=85, optimize=True)
        with open(cached, "rb") as fh:
            data = fh.read()
        mime = "image/jpeg"
    else:
        mime = _image_mime(data[:4])

    return Response(
        content=b"" if request.method == "HEAD" else data,
        media_type=mime,
        headers={"Content-Length": str(len(data)),
                 "Cache-Control": "public, max-age=86400",
                 "X-Tmisc-Source": peer.node_id[:16]},
    )


@router.get("/track/{ident}")
async def track(ident: str, request: Request):
    track_id, info, remote = await _resolve_or_remote(request, parse_ident(ident))
    base = base_url(request)

    import json

    if remote is not None:
        peer, rmeta = remote
        # Las URLs apuntan a ESTE nodo: el cliente reproduce contra su pasarela
        # y no necesita saber (ni poder) hablar con el peer.
        return JSONResponse({
            **(rmeta.get("meta") or {}),
            "id": track_id,
            "stream_url": f"{base}/stream/{track_id}.{rmeta['ext']}",
            "cover_url": f"{base}/cover/{track_id}.jpg",
            "file_url": f"{base}/file/{track_id}",
            "streamable": rmeta["streamable"],
            "source": {"node_id": peer.node_id, "name": peer.name},
        })

    cat = get_catalog(request)
    raw = cat.get_meta_json(track_id)
    meta = json.loads(raw) if raw else {}
    row = cat.resolve(track_id)

    return JSONResponse({
        **meta,
        "id": track_id,
        "stream_url": f"{base}/stream/{track_id}.{row['ext']}",
        "cover_url": f"{base}/cover/{track_id}.jpg",
        "file_url": f"{base}/file/{track_id}",
        "streamable": row["streamable"],
    })


@router.api_route("/file/{ident}", methods=["GET", "HEAD"])
def file(ident: str, request: Request):
    """El fichero entero (.tmisc o audio suelto), con Range. Para P2P y para descargar."""
    track_id, info = _resolve(get_catalog(request), parse_ident(ident))
    total = info.file_size
    plain = info.mode == MODE_PLAIN

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": (info.mime or mime_for_ext(info.ext)) if plain else info.image_mime,
        "ETag": _etag(track_id, info.path, "f-"),
        "Cache-Control": "public, max-age=86400",
        "Content-Disposition":
            f'attachment; filename="{track_id[:16]}.{info.ext if plain else "tmisc"}"',
        "X-Tmisc-Id": track_id,
    }

    return range_response(
        request, total, headers,
        lambda start, length: file_slice(info.path, start, length),
    )
