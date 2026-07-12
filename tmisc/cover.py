"""Portadas — con las guardas de seguridad de Pillow y sin el bug de dimensionado de v1.

EL BUG DE v1 (tmisc.py:188-190): el modo `append` llamaba a `generate_cover()`,
que dimensionaba la imagen para que el audio cupiera EN LOS LSB — capacidad que
append no usa. Resultado: se pagaba un cover gigante y encima se anexaba el audio
otra vez. Un mp3 de 5 MB producía un .tmisc de 57 MB (11.4x), peor que el propio
LSB.

Aquí la portada de un contenedor append tiene TAMAÑO FIJO (500x500 por defecto),
porque es decorativa: no transporta nada. 0.72 MB constantes.
"""

import io
import warnings

import numpy as np
from PIL import Image

from . import format as fmt
from .errors import TmiscError

# Guardas contra bombas de descompresión. Por defecto Pillow solo EMITE un
# warning por encima de 89 Mpx; aquí se convierte en excepción, porque un PNG de
# 1 KB puede declarar 40000x40000 y reventar la RAM al hacer .convert("RGB").
Image.MAX_IMAGE_PIXELS = 64_000_000
warnings.simplefilter("error", Image.DecompressionBombWarning)

ALLOWED_FORMATS = {"PNG", "JPEG", "BMP", "WEBP", "GIF", "TIFF"}

DEFAULT_SIDE = 500          # 500x500x24bpp = 0.72 MB
MAX_COVER_SIDE = 2000

CONTAINER_BY_NAME = {
    "bmp": (fmt.CONTAINER_BMP, "BMP", "image/bmp"),
    "png": (fmt.CONTAINER_PNG, "PNG", "image/png"),
    "jpeg": (fmt.CONTAINER_JPEG, "JPEG", "image/jpeg"),
    "jpg": (fmt.CONTAINER_JPEG, "JPEG", "image/jpeg"),
}


def load_cover(data: bytes) -> Image.Image:
    """Abre una imagen no confiable con todas las guardas puestas."""
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise TmiscError("El cover no es una imagen válida.")

    if img.format not in ALLOWED_FORMATS:
        raise TmiscError(f"Formato de imagen no admitido: {img.format}.")

    w, h = img.size
    if w * h > Image.MAX_IMAGE_PIXELS:
        raise TmiscError(f"Cover demasiado grande: {w}x{h}.")

    try:
        img.load()          # aquí es donde una bomba explotaría; ya está acotada
    except Image.DecompressionBombWarning:
        raise TmiscError("Cover rechazado: bomba de descompresión.")
    except Exception:
        raise TmiscError("No se pudo decodificar el cover.")
    return img


def prepare_cover(img: Image.Image, side: int = DEFAULT_SIDE, depth: int = 24,
                  container: str = "bmp", quality: int = 85) -> tuple[bytes, int, int, int, int]:
    """Normaliza la portada y la serializa.

    Devuelve (bytes, ancho, alto, profundidad, id_de_contenedor).

    `depth=8` produce un BMP paletizado (0.24 MB en vez de 0.72): sigue siendo un
    BMP que abre cualquier visor, a cambio de banding. `container="jpeg"` baja a
    ~60 KB — PNG y JPEG también toleran datos anexados, así que el truco del
    contenedor funciona igual.
    """
    side = max(16, min(side, MAX_COVER_SIDE))
    img = img.convert("RGB")
    if max(img.size) != side or img.width != img.height:
        img = _fit_square(img, side)

    cid, pil_format, _ = CONTAINER_BY_NAME.get(container.lower(), CONTAINER_BY_NAME["bmp"])

    if pil_format == "BMP" and depth == 8:
        img = img.convert("P", palette=Image.ADAPTIVE, colors=256)

    buf = io.BytesIO()
    if pil_format == "JPEG":
        img.save(buf, format="JPEG", quality=quality, optimize=True)
    else:
        img.save(buf, format=pil_format)

    return buf.getvalue(), img.width, img.height, depth, cid


def _fit_square(img: Image.Image, side: int) -> Image.Image:
    """Recorta al centro y escala a un cuadrado, sin deformar."""
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
    return img.resize((side, side), Image.LANCZOS)


def generate_identicon(seed: bytes, side: int = DEFAULT_SIDE) -> Image.Image:
    """Portada autogenerada cuando no se aporta ninguna: una huella del audio.

    Derivada del BLAKE3, así que el mismo audio produce siempre la misma imagen.
    Tamaño FIJO: no depende de la longitud del audio (ese era el bug de v1).
    """
    if len(seed) < 32:
        seed = (seed * 32)[:32] or b"\x00" * 32

    cells = 8
    rnd = np.frombuffer(seed[:32], dtype=np.uint8)

    # Dos colores del propio hash, con luminosidad forzada para que contrasten.
    fg = np.array([rnd[0] | 0x40, rnd[1] | 0x40, rnd[2] | 0x40], dtype=np.uint8)
    bg = np.array([rnd[3] >> 2, rnd[4] >> 2, rnd[5] >> 2], dtype=np.uint8)

    # Rejilla simétrica respecto al eje vertical, como un identicon de GitHub.
    half = (cells + 1) // 2
    bits = np.unpackbits(rnd[6:6 + ((half * cells + 7) // 8)])[: half * cells]
    left = bits.reshape(cells, half)
    grid = np.concatenate([left, left[:, ::-1][:, cells % 2:]], axis=1)

    img = np.where(grid[..., None].astype(bool), fg, bg).astype(np.uint8)
    return Image.fromarray(img, "RGB").resize((side, side), Image.NEAREST)
