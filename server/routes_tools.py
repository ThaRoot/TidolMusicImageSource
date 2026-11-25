"""/encode, /decode, /probe, /capacity — disponibles en ambos perfiles.

Estos endpoints no sirven la biblioteca: transforman lo que se les sube. Son los
únicos que podrían vivir fuera de la LAN, aunque la recomendación es no
exponerlos a Internet (ver README, "Despliegue").
"""

import io
import os
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from tmisc import decode as dec
from tmisc import encode as enc
from tmisc.container import probe
from tmisc.errors import CapacityError, NotTmiscError, TmiscError
from tmisc.meta import AudioMeta, TrackMeta
from tmisc.sniff import safe_ext

from .deps import get_settings

router = APIRouter()

READ_CHUNK = 1 << 20


async def _read_limited(upload: UploadFile, limit: int, what: str) -> bytes:
    """Lee un upload con tope real.

    El bug de v1: `await file.read()` sin argumento materializa el upload entero
    en RAM, y en /decode no había ningún límite. Un POST de 2 GB tumbaba el
    proceso. Aquí se lee por trozos y se aborta en cuanto se pasa del tope.
    """
    buf = bytearray()
    while True:
        chunk = await upload.read(READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, f"{what} supera el límite de {limit // (1 << 20)} MB.")
    if not buf:
        raise HTTPException(400, f"{what} está vacío.")
    return bytes(buf)


def _guard_content_length(request: Request, limit: int) -> None:
    """Rechaza de inmediato, sin leer un solo byte del cuerpo."""
    raw = request.headers.get("content-length")
    if raw and raw.isdigit() and int(raw) > limit + (8 << 20):   # holgura del multipart
        raise HTTPException(413, f"La petición supera el límite de {limit // (1 << 20)} MB.")


@router.post("/encode")
async def encode(
    request: Request,
    audio: UploadFile = File(..., description="Archivo de audio a empaquetar"),
    cover: UploadFile | None = File(None, description="Portada (opcional)"),
    mode: str = Form("append", description="append | lsb"),
    title: str | None = Form(None),
    artist: str | None = Form(None),
    album: str | None = Form(None),
    cover_side: int = Form(500),
    cover_depth: int = Form(24),
    cover_container: str = Form("bmp"),
):
    settings = get_settings(request)
    _guard_content_length(request, settings.max_upload)

    if mode not in ("append", "lsb"):
        raise HTTPException(400, "mode debe ser 'append' o 'lsb'.")

    audio_bytes = await _read_limited(audio, settings.max_upload, "El audio")
    cover_bytes = await _read_limited(cover, 32 << 20, "El cover") if cover is not None else None

    if mode == "lsb":
        # Legacy: 10.4x de overhead y sin seek. Se mantiene por compatibilidad.
        from PIL import Image
        from tmisc.cover import load_cover
        from tmisc.legacy_lsb import encode_lsb, generate_cover_for_lsb

        img: Image.Image = (
            load_cover(cover_bytes) if cover_bytes else generate_cover_for_lsb(audio_bytes)
        )
        try:
            data = encode_lsb(img, audio_bytes, safe_ext((audio.filename or "").rsplit(".", 1)[-1]))
        except CapacityError as e:
            raise HTTPException(413, str(e))
        used_mode = "lsb"
    else:
        try:
            data = enc.encode_bytes(
                audio_bytes,
                cover_data=cover_bytes,
                track=TrackMeta(title=title, artist=artist, album=album),
                audio_meta=AudioMeta(),
                source_filename=audio.filename,
                side=cover_side, depth=cover_depth, container=cover_container,
            )
        except TmiscError as e:
            raise HTTPException(400, str(e))
        used_mode = "append-v2"

    ratio = round(len(data) / len(audio_bytes), 3)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="image/bmp",
        headers={
            "Content-Disposition": 'attachment; filename="output.tmisc"',
            "Content-Length": str(len(data)),
            "X-Tmisc-Mode": used_mode,
            "X-Tmisc-Overhead": str(ratio),
        },
    )


@router.post("/decode")
async def decode(request: Request, file: UploadFile = File(..., description="Archivo .tmisc")):
    settings = get_settings(request)
    _guard_content_length(request, settings.max_upload)
    data = await _read_limited(file, settings.max_upload, "El archivo")

    try:
        audio, ext, mode = dec.decode_bytes(data)
    except NotTmiscError:
        raise HTTPException(422, "El archivo no contiene audio TMISC.")
    except TmiscError as e:
        raise HTTPException(422, str(e))

    # `ext` está saneado (^[a-z0-9]{1,8}$) y sale de los magic bytes del audio,
    # no del nombre del fichero subido. En v1 salía del nombre y, como \r y \n
    # son ASCII y sobrevivían al filtro, se podían inyectar cabeceras aquí.
    ext = safe_ext(ext)
    return StreamingResponse(
        io.BytesIO(audio),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="recovered.{ext}"',
            "Content-Length": str(len(audio)),
            "X-Tmisc-Mode": mode,
        },
    )


@router.post("/probe")
async def probe_endpoint(request: Request, file: UploadFile = File(...)):
    """Info del footer sin extraer el audio. Lee poco más de 120 bytes."""
    settings = get_settings(request)
    _guard_content_length(request, settings.max_upload)
    data = await _read_limited(file, settings.max_upload, "El archivo")

    with tempfile.NamedTemporaryFile(suffix=".tmisc", delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        info = probe(path)
        return JSONResponse({
            "version": info.version,
            "mode": dec.mode_name(info),
            "streamable": info.streamable,
            "file_size": info.file_size,
            "image_size": info.image_size,
            "audio_offset": info.audio_offset,
            "audio_size": info.audio_size,
            "overhead": round(info.file_size / info.audio_size, 3) if info.audio_size else None,
            "mime": info.mime,
            "ext": info.ext,
            "id": info.track_id,
        })
    except NotTmiscError:
        raise HTTPException(422, "El archivo no contiene audio TMISC.")
    except TmiscError as e:
        raise HTTPException(422, str(e))
    finally:
        os.unlink(path)


@router.post("/capacity")
async def capacity(cover: UploadFile = File(...)):
    """Capacidad LSB de una imagen. Legacy: solo aplica al modo `lsb`."""
    from tmisc.cover import load_cover
    from tmisc.legacy_lsb import LSB_HEADER_SIZE, lsb_capacity_bytes

    data = await _read_limited(cover, 32 << 20, "El cover")
    img = load_cover(data).convert("RGB")
    w, h = img.size
    cap = max(0, lsb_capacity_bytes(w, h) - LSB_HEADER_SIZE)
    return JSONResponse({
        "width": w, "height": h,
        "lsb_capacity_bytes": cap,
        "lsb_capacity_kb": round(cap / 1024, 1),
        "nota": "Solo relevante en modo lsb. El modo append no tiene límite de "
                "capacidad y pesa ~10x menos.",
    })
