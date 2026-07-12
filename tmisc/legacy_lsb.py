"""Modo LSB — legacy, aislado aquí.

Esconde el audio en los bits menos significativos de los píxeles. Se conserva
para leer los .tmisc v1 que ya existan, y como curiosidad, pero NO es el camino
de producción:

  - Cuesta 10.4x el tamaño del audio (24 bits de píxel por cada 3 de payload,
    más el headroom del cover autogenerado).
  - No se puede streamear: para servir un solo byte hay que decodificar todos
    los píxeles. El gateway lo resuelve materializando el audio en caché la
    primera vez.
  - No sobrevive a que un editor reexporte la imagen.

En una LAN, además, no protege de nadie: no hay adversario entre el teléfono y
la rocola del que haya que esconder la música.
"""

import io
import struct
import zlib

import numpy as np
from PIL import Image

from .cover import load_cover
from .errors import CapacityError, ChecksumError, NotTmiscError

MAGIC = b"TMISC1"
VERSION = 1
MODE_LSB = 1

# magic(6) | version(1) | mode(1) | fmt(8 ascii) | audio_size(u64) | crc32(u32)
LSB_HEADER_FMT = ">6sBB8sQI"
LSB_HEADER_SIZE = struct.calcsize(LSB_HEADER_FMT)   # 28


def lsb_capacity_bytes(width: int, height: int) -> int:
    """Payload que cabe usando 1 bit por canal RGB."""
    return (width * height * 3) // 8


def _fmt_field(ext: str) -> bytes:
    b = (ext or "bin").encode("ascii", "ignore")[:8]
    return b + b"\x00" * (8 - len(b))


def encode_lsb(cover: Image.Image, audio: bytes, ext: str) -> bytes:
    arr = np.asarray(cover.convert("RGB"), dtype=np.uint8)
    flat = arr.reshape(-1).copy()

    header = struct.pack(LSB_HEADER_FMT, MAGIC, VERSION, MODE_LSB,
                         _fmt_field(ext), len(audio), zlib.crc32(audio) & 0xFFFFFFFF)
    bits = np.unpackbits(np.frombuffer(header + audio, dtype=np.uint8))

    if bits.size > flat.size:
        need_px = -(-bits.size // 3)
        raise CapacityError(
            f"El audio no cabe en LSB: necesita ~{need_px} px y la imagen tiene "
            f"{flat.size // 3}. Usa un cover más grande, o el modo append (que "
            f"además pesa 10x menos)."
        )

    flat[: bits.size] = (flat[: bits.size] & 0xFE) | bits
    out = io.BytesIO()
    Image.fromarray(flat.reshape(arr.shape), "RGB").save(out, format="BMP")
    return out.getvalue()


def decode_lsb(img: Image.Image) -> tuple[bytes, str]:
    flat = np.asarray(img.convert("RGB"), dtype=np.uint8).reshape(-1)
    lsb = (flat & 1).astype(np.uint8)

    header_bits = LSB_HEADER_SIZE * 8
    if lsb.size < header_bits:
        raise NotTmiscError("Imagen demasiado pequeña para contener una cabecera TMISC.")

    magic, _ver, mode, fmt_f, size, crc = struct.unpack(
        LSB_HEADER_FMT, np.packbits(lsb[:header_bits]).tobytes()
    )
    if magic != MAGIC or mode != MODE_LSB:
        raise NotTmiscError("No se encontró una cabecera LSB de TMISC.")

    total_bits = (LSB_HEADER_SIZE + size) * 8
    if lsb.size < total_bits:
        raise NotTmiscError("Datos LSB truncados: la imagen fue recomprimida o recortada.")

    payload = np.packbits(lsb[:total_bits]).tobytes()
    audio = payload[LSB_HEADER_SIZE: LSB_HEADER_SIZE + size]
    if zlib.crc32(audio) & 0xFFFFFFFF != crc:
        raise ChecksumError("CRC del audio no coincide (imagen alterada).")

    ext = fmt_f.split(b"\x00")[0].decode("ascii", "ignore") or "bin"
    return audio, ext


def generate_cover_for_lsb(audio: bytes, headroom: float = 1.30) -> Image.Image:
    """Cover dimensionado para que el audio quepa en los LSB.

    Este dimensionado-por-capacidad es correcto AQUÍ y solo aquí. Aplicarlo al
    modo append era el bug de v1 (tmisc.py:188-190): append no usa los LSB, así
    que la imagen enorme era puro desperdicio.
    """
    needed = int((LSB_HEADER_SIZE + len(audio)) * headroom)
    pixels = -(-(needed * 8) // 3)
    side = max(16, int(np.ceil(np.sqrt(pixels))))

    src = np.frombuffer(audio or b"\x00", dtype=np.uint8)
    tiled = np.tile(src, -(-(side * side) // src.size))[: side * side].astype(np.uint8)
    rgb = np.stack(
        [tiled, np.roll(tiled, side // 3), np.roll(tiled, (2 * side) // 3)], axis=1
    ).reshape(side, side, 3)
    return Image.fromarray(rgb, "RGB")


# ---------------------------------------------------------------------------
# Enganche con el catálogo y el gateway
# ---------------------------------------------------------------------------
def probe_lsb(path: str):
    """¿Es un .tmisc con audio en los píxeles? Devuelve TmiscInfo o None.

    Caro: hay que decodificar la imagen. Solo se llama cuando ya se descartó el
    footer v2 y el trailer v1.
    """
    from .container import TmiscInfo   # import perezoso: evita el ciclo
    from .format import MODE_LSB_V1
    from .sniff import mime_for_ext

    import os

    try:
        with open(path, "rb") as fh:
            img = load_cover(fh.read())
        audio, ext = decode_lsb(img)
    except Exception:
        return None

    st = os.stat(path)
    return TmiscInfo(
        path=path, file_size=st.st_size, version=1, mode=MODE_LSB_V1,
        audio_offset=0, audio_size=len(audio), image_size=st.st_size,
        ext=ext, mime=mime_for_ext(ext), mtime_ns=st.st_mtime_ns,
    )


def extract_lsb_audio(info) -> bytes:
    """Decodifica el audio escondido en los píxeles. Se cachea aguas arriba."""
    with open(info.path, "rb") as fh:
        img = load_cover(fh.read())
    audio, _ = decode_lsb(img)
    return audio
