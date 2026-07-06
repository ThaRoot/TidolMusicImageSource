"""
main.py — Microservicio TMISC (FastAPI).

Endpoints:
  GET  /health          -> estado
  GET  /                -> info + capacidad
  POST /encode          -> audio (+cover opcional) => archivo .tmisc (BMP)
  POST /decode          -> .tmisc/.bmp => audio original
  POST /capacity        -> calcula capacidad LSB de un cover

Ejecutar:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import io

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image

import tmisc

app = FastAPI(
    title="TMISC — Audio-in-Image Steganography Service",
    version="1.0.0",
    description="Codifica audio dentro de imágenes BMP (.tmisc).",
)

MAX_BYTES = 100 * 1024 * 1024  # 100 MB por archivo


def _open_image(data: bytes) -> Image.Image:
    try:
        return Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "El cover no es una imagen válida.")


@app.get("/health")
def health():
    return {"status": "ok", "service": "tmisc", "version": "1.0.0"}


@app.get("/")
def root():
    return {
        "service": "tmisc",
        "formato": ".tmisc (BMP válido con audio oculto)",
        "modos": {
            "lsb": "esteganografía real en bits menos significativos (capacidad limitada)",
            "append": "audio anexado tras el BMP (capacidad ilimitada)",
            "auto": "LSB si cabe en el cover, si no append",
        },
        "endpoints": ["/encode", "/decode", "/capacity", "/health"],
    }


@app.post("/encode")
async def encode(
    audio: UploadFile = File(..., description="Archivo de audio a ocultar"),
    cover: UploadFile | None = File(None, description="Imagen cover (opcional)"),
    mode: str = Form("auto", description="auto | lsb | append"),
):
    if mode not in ("auto", "lsb", "append"):
        raise HTTPException(400, "mode debe ser auto, lsb o append.")

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(400, "El audio está vacío.")
    if len(audio_bytes) > MAX_BYTES:
        raise HTTPException(413, "Audio demasiado grande.")

    # formato = extensión del archivo original (para restaurarlo al decodificar)
    fmt = (audio.filename or "audio.bin").rsplit(".", 1)[-1] if audio.filename else "bin"

    cover_img = None
    if cover is not None:
        cover_img = _open_image(await cover.read())

    try:
        data, used_mode = tmisc.encode(audio_bytes, fmt, cover=cover_img, mode=mode)
    except tmisc.CapacityError as e:
        raise HTTPException(413, str(e))
    except tmisc.TmiscError as e:
        raise HTTPException(400, str(e))

    return StreamingResponse(
        io.BytesIO(data),
        media_type="image/bmp",
        headers={
            "Content-Disposition": 'attachment; filename="output.tmisc"',
            "X-Tmisc-Mode": used_mode,
            "X-Tmisc-Audio-Format": fmt,
            "X-Tmisc-Size": str(len(data)),
        },
    )


@app.post("/decode")
async def decode(file: UploadFile = File(..., description="Archivo .tmisc o .bmp")):
    data = await file.read()
    if not data:
        raise HTTPException(400, "El archivo está vacío.")
    try:
        audio, fmt, mode = tmisc.decode(data)
    except tmisc.NotTmiscError:
        raise HTTPException(422, "El archivo no contiene audio TMISC.")
    except tmisc.ChecksumError as e:
        raise HTTPException(422, f"Integridad: {e}")
    except tmisc.TmiscError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "No se pudo procesar el archivo.")

    return StreamingResponse(
        io.BytesIO(audio),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="recovered.{fmt}"',
            "X-Tmisc-Mode": mode,
            "X-Tmisc-Audio-Format": fmt,
        },
    )


@app.post("/capacity")
async def capacity(cover: UploadFile = File(...)):
    img = _open_image(await cover.read()).convert("RGB")
    w, h = img.size
    cap = tmisc.lsb_capacity_bytes(w, h) - tmisc.LSB_HEADER_SIZE
    return JSONResponse({
        "width": w,
        "height": h,
        "lsb_capacity_bytes": max(0, cap),
        "lsb_capacity_kb": round(max(0, cap) / 1024, 1),
        "nota": "Audios mayores a esto usarán modo append automáticamente.",
    })
