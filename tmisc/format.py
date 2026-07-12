"""Footer binario de .tmisc v2 — la especificación normativa está en SPEC.md.

Layout físico del archivo:

    ┌────────────────────────────────┐ 0
    │ BMP (cover real)               │  image_size  (== bfSize del BMP)
    ├────────────────────────────────┤ audio_offset
    │ AUDIO (bytes originales)       │  audio_size
    ├────────────────────────────────┤ meta_offset
    │ META (JSON UTF-8 compacto)     │  meta_size
    ├────────────────────────────────┤ file_size - 120
    │ FOOTER (112 B body + 8 B tail) │
    └────────────────────────────────┘ EOF

El audio va ANTES que los metadatos a propósito: los metadatos son la parte
mutable (retaggear, añadir MBID, añadir letra) y el audio es la content-address.
Con este orden, retaggear = truncar en meta_offset y reescribir JSON + footer;
el audio no se mueve, así que audio_offset y el hash —y por tanto el id— no
cambian.

El footer es BIG-ENDIAN. El BMP que lo precede es little-endian porque lo manda
su spec. Es la trampa número uno para quien implemente un lector en otro
lenguaje: no se hereda el endianness del contenedor.

Los últimos 8 bytes son un tail auto-localizable (magic + tamaño del footer),
igual que el EOCD de un ZIP: una v2.1 con un footer más grande seguirá siendo
localizable por un lector v2.0, que sabrá dónde empieza aunque lo rechace.
"""

import struct
import time
import zlib
from dataclasses import dataclass, field

from .errors import CorruptFooterError, UnsupportedVersionError

MAGIC_V2 = b"TMISC2"
MAGIC_V1 = b"TMISC1"

VERSION_MAJOR = 2
VERSION_MINOR = 0

# --- Modos --------------------------------------------------------------------
MODE_LSB_V1 = 1        # legacy: audio en los bits menos significativos
MODE_APPEND_V1 = 2     # legacy: BMP + audio + trailer de 27 B
MODE_APPEND_V2 = 3     # el modo de producción
MODE_LSB_V2 = 4        # LSB con footer v2 (indexable, pero no streameable)

# --- Flags (u8) ---------------------------------------------------------------
FLAG_ENCRYPTED = 0x01          # reservado; en v2.0 siempre 0
FLAG_HAS_META = 0x02
FLAG_LSB_PAYLOAD = 0x04        # el audio está en los píxeles: NO es un slice
FLAG_AUDIO_TRANSCODED = 0x08   # el segmento no es byte-idéntico al original
FLAG_HAS_SEEK_INDEX = 0x10     # reservado: tabla de frames en reserved1

# --- Contenedor de imagen (u8) ------------------------------------------------
CONTAINER_BMP = 1      # normativo
CONTAINER_PNG = 2
CONTAINER_JPEG = 3

CONTAINER_MIME = {
    CONTAINER_BMP: "image/bmp",
    CONTAINER_PNG: "image/png",
    CONTAINER_JPEG: "image/jpeg",
}

# ---------------------------------------------------------------------------
# FOOTER v2
# ---------------------------------------------------------------------------
# off  tam  campo
#   0    1  version_major
#   1    1  version_minor
#   2    1  mode
#   3    1  flags
#   4    1  container   (+ 3 bytes de padding reservado)
#   8    8  file_size      <- detecta truncamiento en O(1)
#  16    8  image_size
#  24    8  audio_offset   <- el que hace posible el seek O(1)
#  32    8  audio_size
#  40    8  meta_offset
#  48    4  meta_size
#  52    4  audio_crc32
#  56   32  audio_blake3   <- el id del track
#  88    4  meta_crc32
#  92    8  created_unix
# 100    8  reserved1
# 108    4  footer_crc32   <- crc de body[0:108]
FOOTER_BODY_FMT = ">BBBBB3xQQQQQII32sIQ8sI"
FOOTER_BODY_SIZE = struct.calcsize(FOOTER_BODY_FMT)      # 112

FOOTER_TAIL_FMT = ">6sH"                                  # magic + footer_size
FOOTER_TAIL_SIZE = struct.calcsize(FOOTER_TAIL_FMT)       # 8

FOOTER_SIZE = FOOTER_BODY_SIZE + FOOTER_TAIL_SIZE         # 120
CRC_COVERED = FOOTER_BODY_SIZE - 4                        # 108

# --- Trailer v1 (solo lectura, para compatibilidad) ---------------------------
# magic(6) | mode(1) | fmt(8 ascii) | audio_size(u64) | crc32(u32)
TRAILER_V1_FMT = ">6sB8sQI"
TRAILER_V1_SIZE = struct.calcsize(TRAILER_V1_FMT)         # 27

# Un BMP tiene como mínimo un BITMAPFILEHEADER de 14 bytes. Ningún offset de
# audio puede caer por debajo de eso sin solapar la imagen.
MIN_IMAGE_BYTES = 14

assert FOOTER_BODY_SIZE == 112, FOOTER_BODY_SIZE
assert FOOTER_SIZE == 120, FOOTER_SIZE
assert TRAILER_V1_SIZE == 27, TRAILER_V1_SIZE


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class Footer:
    """El footer v2 decodificado. Todos los offsets son absolutos."""

    mode: int = MODE_APPEND_V2
    flags: int = FLAG_HAS_META
    container: int = CONTAINER_BMP
    file_size: int = 0
    image_size: int = 0
    audio_offset: int = 0
    audio_size: int = 0
    meta_offset: int = 0
    meta_size: int = 0
    audio_crc32: int = 0
    audio_blake3: bytes = b"\x00" * 32
    meta_crc32: int = 0
    created_unix: int = field(default_factory=lambda: int(time.time()))
    version_major: int = VERSION_MAJOR
    version_minor: int = VERSION_MINOR
    reserved1: bytes = b"\x00" * 8

    @property
    def track_id(self) -> str:
        return self.audio_blake3.hex()

    @property
    def has_meta(self) -> bool:
        return bool(self.flags & FLAG_HAS_META) and self.meta_size > 0

    @property
    def is_lsb(self) -> bool:
        return bool(self.flags & FLAG_LSB_PAYLOAD) or self.mode in (MODE_LSB_V1, MODE_LSB_V2)

    @property
    def streamable(self) -> bool:
        """¿Se puede servir el audio como un slice del fichero, sin decodificar?"""
        return not self.is_lsb

    def pack(self) -> bytes:
        body = struct.pack(
            FOOTER_BODY_FMT,
            self.version_major, self.version_minor, self.mode, self.flags,
            self.container,
            self.file_size, self.image_size, self.audio_offset, self.audio_size,
            self.meta_offset, self.meta_size, self.audio_crc32,
            self.audio_blake3, self.meta_crc32, self.created_unix,
            self.reserved1,
            0,  # placeholder del propio crc
        )
        body = body[:CRC_COVERED] + struct.pack(">I", crc32(body[:CRC_COVERED]))
        tail = struct.pack(FOOTER_TAIL_FMT, MAGIC_V2, FOOTER_SIZE)
        return body + tail

    @classmethod
    def unpack(cls, body: bytes) -> "Footer":
        if len(body) < FOOTER_BODY_SIZE:
            raise CorruptFooterError("Footer truncado.")
        body = body[:FOOTER_BODY_SIZE]

        (vmaj, vmin, mode, flags, container,
         file_size, image_size, audio_offset, audio_size,
         meta_offset, meta_size, audio_crc,
         blake, meta_crc, created, reserved1, stored_crc) = struct.unpack(FOOTER_BODY_FMT, body)

        # El crc del propio footer se valida ANTES de creer ningún offset: un
        # footer corrupto no debe poder apuntar a cualquier sitio del archivo.
        if crc32(body[:CRC_COVERED]) != stored_crc:
            raise CorruptFooterError("CRC del footer no coincide.")

        if vmaj != VERSION_MAJOR:
            raise UnsupportedVersionError(
                f"Footer TMISC v{vmaj}.{vmin}; este lector entiende v{VERSION_MAJOR}.x."
            )

        return cls(
            mode=mode, flags=flags, container=container,
            file_size=file_size, image_size=image_size,
            audio_offset=audio_offset, audio_size=audio_size,
            meta_offset=meta_offset, meta_size=meta_size,
            audio_crc32=audio_crc, audio_blake3=blake, meta_crc32=meta_crc,
            created_unix=created, version_major=vmaj, version_minor=vmin,
            reserved1=reserved1,
        )

    def validate_against(self, actual_size: int) -> None:
        """Coherencia de los offsets contra el tamaño real del fichero.

        Sin esto, un footer manipulado podría hacer que /stream devolviera
        bytes del BMP, o del propio footer, o de fuera del archivo.
        """
        if self.file_size != actual_size:
            raise CorruptFooterError(
                f"El footer declara {self.file_size} B pero el archivo tiene "
                f"{actual_size} B (¿truncado o aún copiándose?)."
            )

        footer_start = actual_size - FOOTER_SIZE

        if self.is_lsb:
            # En LSB los offsets describen el payload lógico dentro de los
            # píxeles, no un slice del fichero: no hay nada que validar aquí.
            return

        if self.audio_offset < MIN_IMAGE_BYTES:
            raise CorruptFooterError("audio_offset solapa la cabecera de la imagen.")
        if self.audio_size < 0 or self.audio_offset + self.audio_size > footer_start:
            raise CorruptFooterError("El segmento de audio se sale del archivo.")
        if self.meta_size:
            if self.meta_offset < self.audio_offset + self.audio_size:
                raise CorruptFooterError("El segmento de metadatos solapa el audio.")
            if self.meta_offset + self.meta_size > footer_start:
                raise CorruptFooterError("El segmento de metadatos se sale del archivo.")
        if self.image_size > self.audio_offset:
            raise CorruptFooterError("image_size solapa el audio.")


def bmp_declared_size(head: bytes) -> int | None:
    """`bfSize` de un BMP: dónde acaba la imagen, sin necesidad de parsear el DIB.

    Pillow lo escribe igual a la longitud del BMP (no del .tmisc), así que sirve
    para servir /cover como el slice [0, bfSize) sin abrir Pillow siquiera.
    Ojo: little-endian, a diferencia del footer.
    """
    if len(head) < 14 or head[:2] != b"BM":
        return None
    return struct.unpack("<I", head[2:6])[0]
