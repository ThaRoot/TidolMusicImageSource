"""
tmisc.py — Núcleo del formato .tmisc (esteganografía de audio en imagen).

Un archivo .tmisc SIEMPRE es un BMP válido (se abre en cualquier visor de
imágenes). El audio va oculto de una de dos formas:

  MODE_LSB    -> Esteganografía real: el audio se esconde en los bits menos
                 significativos (LSB) de los píxeles. La imagen se ve casi
                 idéntica. Capacidad limitada: ~3 bits por píxel.

  MODE_APPEND -> El audio se anexa DESPUÉS de los datos del BMP, con un
                 "trailer" al final. Los visores leen solo lo que declara la
                 cabecera BMP e ignoran el resto, así que la imagen sigue viéndose
                 bien. Capacidad ilimitada (pero es "ocultar" más que "esteganografía").

El decodificador detecta el modo solo: primero mira el trailer (últimos 27 bytes);
si el magic coincide es APPEND; si no, intenta leer la cabecera desde los LSB.
"""

import io
import struct
import zlib

import numpy as np
from PIL import Image

MAGIC = b"TMISC1"          # 6 bytes de firma del formato
VERSION = 1

MODE_LSB = 1
MODE_APPEND = 2

# --- Cabecera embebida (modo LSB), va dentro de los píxeles -------------------
# magic(6) | version(1) | mode(1) | fmt(8, ascii null-padded) | audio_size(u64) | crc32(u32)
LSB_HEADER_FMT = ">6sBB8sQI"
LSB_HEADER_SIZE = struct.calcsize(LSB_HEADER_FMT)   # 28 bytes

# --- Trailer (modo APPEND), va al final físico del archivo --------------------
# magic(6) | mode(1) | fmt(8, ascii null-padded) | audio_size(u64) | crc32(u32)
TRAILER_FMT = ">6sB8sQI"
TRAILER_SIZE = struct.calcsize(TRAILER_FMT)         # 27 bytes


class TmiscError(Exception):
    pass


class CapacityError(TmiscError):
    pass


class NotTmiscError(TmiscError):
    pass


class ChecksumError(TmiscError):
    pass


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def _fmt_field(fmt: str) -> bytes:
    b = (fmt or "bin").lower().encode("ascii", "ignore")[:8]
    return b + b"\x00" * (8 - len(b))


def _read_fmt_field(b: bytes) -> str:
    return b.split(b"\x00")[0].decode("ascii", "ignore") or "bin"


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def lsb_capacity_bytes(width: int, height: int) -> int:
    """Cuántos bytes de payload (cabecera + audio) caben usando 1 LSB por canal RGB."""
    total_bits = width * height * 3
    return total_bits // 8


# ---------------------------------------------------------------------------
# Modo LSB — esteganografía real en los píxeles
# ---------------------------------------------------------------------------
def encode_lsb(cover: Image.Image, audio: bytes, fmt: str) -> Image.Image:
    img = cover.convert("RGB")
    arr = np.asarray(img, dtype=np.uint8)
    flat = arr.reshape(-1).copy()

    header = struct.pack(LSB_HEADER_FMT, MAGIC, VERSION, MODE_LSB,
                         _fmt_field(fmt), len(audio), _crc(audio))
    payload = header + audio
    bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8))

    if bits.size > flat.size:
        need_px = -(-bits.size // 3)  # ceil
        raise CapacityError(
            f"El audio no cabe en LSB: necesita ~{need_px} px, "
            f"la imagen tiene {flat.size // 3}. Usa un cover más grande o modo append."
        )

    flat[:bits.size] = (flat[:bits.size] & 0xFE) | bits
    return Image.fromarray(flat.reshape(arr.shape), "RGB")


def decode_lsb(img: Image.Image) -> tuple[bytes, str]:
    flat = np.asarray(img.convert("RGB"), dtype=np.uint8).reshape(-1)
    lsb = (flat & 1).astype(np.uint8)

    header_bits = LSB_HEADER_SIZE * 8
    if lsb.size < header_bits:
        raise NotTmiscError("Imagen demasiado pequeña para contener una cabecera TMISC.")

    header = np.packbits(lsb[:header_bits]).tobytes()
    magic, ver, mode, fmt_f, size, crc = struct.unpack(LSB_HEADER_FMT, header)
    if magic != MAGIC or mode != MODE_LSB:
        raise NotTmiscError("No se encontró una cabecera LSB de TMISC válida.")

    total_bits = (LSB_HEADER_SIZE + size) * 8
    if lsb.size < total_bits:
        raise TmiscError("Datos LSB truncados; la imagen fue recomprimida o recortada.")

    payload = np.packbits(lsb[:total_bits]).tobytes()
    audio = payload[LSB_HEADER_SIZE:LSB_HEADER_SIZE + size]
    if _crc(audio) != crc:
        raise ChecksumError("CRC del audio no coincide (imagen alterada).")
    return audio, _read_fmt_field(fmt_f)


# ---------------------------------------------------------------------------
# Modo APPEND — BMP válido + audio anexado + trailer
# ---------------------------------------------------------------------------
def encode_append(cover: Image.Image, audio: bytes, fmt: str) -> bytes:
    buf = io.BytesIO()
    cover.convert("RGB").save(buf, format="BMP")
    bmp = buf.getvalue()
    trailer = struct.pack(TRAILER_FMT, MAGIC, MODE_APPEND,
                          _fmt_field(fmt), len(audio), _crc(audio))
    return bmp + audio + trailer


def decode_append(data: bytes) -> tuple[bytes, str] | None:
    if len(data) < TRAILER_SIZE:
        return None
    trailer = data[-TRAILER_SIZE:]
    magic, mode, fmt_f, size, crc = struct.unpack(TRAILER_FMT, trailer)
    if magic != MAGIC or mode != MODE_APPEND:
        return None
    start = len(data) - TRAILER_SIZE - size
    if start < 0:
        raise TmiscError("Trailer TMISC apunta fuera del archivo.")
    audio = data[start:len(data) - TRAILER_SIZE]
    if _crc(audio) != crc:
        raise ChecksumError("CRC del audio no coincide (archivo alterado).")
    return audio, _read_fmt_field(fmt_f)


# ---------------------------------------------------------------------------
# Cover autogenerado a partir del propio audio (huella visual)
# ---------------------------------------------------------------------------
def generate_cover(audio: bytes, headroom: float = 1.30) -> Image.Image:
    """Crea una imagen cuadrada con capacidad LSB suficiente para el audio.

    La parte visible se deriva de los bytes del audio (una "huella sonora"),
    y luego encode_lsb guardará el audio exacto en los LSB.
    """
    needed_bytes = int((LSB_HEADER_SIZE + len(audio)) * headroom)
    pixels = -(-(needed_bytes * 8) // 3)          # ceil bits->px
    side = max(16, int(np.ceil(np.sqrt(pixels))))

    src = np.frombuffer(audio or b"\x00", dtype=np.uint8)
    reps = -(-(side * side) // src.size)
    tiled = np.tile(src, reps)[: side * side].astype(np.uint8)

    r = tiled
    g = np.roll(tiled, side // 3)
    b = np.roll(tiled, (2 * side) // 3)
    rgb = np.stack([r, g, b], axis=1).reshape(side, side, 3)
    return Image.fromarray(rgb, "RGB")


# ---------------------------------------------------------------------------
# API de alto nivel
# ---------------------------------------------------------------------------
def encode(audio: bytes, fmt: str, cover: Image.Image | None = None,
           mode: str = "auto") -> tuple[bytes, str]:
    """Devuelve (bytes_del_archivo_tmisc, modo_usado)."""
    if mode == "append":
        cover = cover or generate_cover(audio)
        return encode_append(cover, audio, fmt), "append"

    if mode == "lsb":
        cover = cover or generate_cover(audio)
        return _img_to_bmp_bytes(encode_lsb(cover, audio, fmt)), "lsb"

    # auto
    if cover is None:
        cover = generate_cover(audio)
        return _img_to_bmp_bytes(encode_lsb(cover, audio, fmt)), "lsb"

    w, h = cover.size
    if lsb_capacity_bytes(w, h) >= LSB_HEADER_SIZE + len(audio):
        return _img_to_bmp_bytes(encode_lsb(cover, audio, fmt)), "lsb"
    return encode_append(cover, audio, fmt), "append"


def decode(data: bytes) -> tuple[bytes, str, str]:
    """Devuelve (audio_bytes, formato_audio, modo_detectado)."""
    appended = decode_append(data)
    if appended is not None:
        audio, fmt = appended
        return audio, fmt, "append"
    img = Image.open(io.BytesIO(data))
    audio, fmt = decode_lsb(img)
    return audio, fmt, "lsb"


def _img_to_bmp_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()
