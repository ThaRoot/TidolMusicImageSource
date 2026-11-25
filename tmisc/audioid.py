"""Id de contenido: BLAKE3 del audio SIN etiquetas.

El id de un track tiene que sobrevivir a que alguien edite sus etiquetas (Picard,
beets, el reproductor del móvil): si cambiara, la deduplicación y la verificación
P2P se romperían con una simple corrección de un título. Por eso NO es el hash
del fichero, sino el de los bytes que de verdad suenan:

  mp3 / aac   sin ID3v2 (al principio), ni ID3v1 / APEv2 / Lyrics3 (al final)
  flac        solo los frames: sin "fLaC" ni bloques de metadatos (tags, PICTURE, padding)
  ogg / opus  los paquetes de audio: fuera los de cabecera (OpusTags / comentarios de
              Vorbis, aunque la portada embebida ocupe varias páginas). Se hashea el
              contenido de los paquetes, no las páginas, porque reescribir los tags
              cambia la numeración y el CRC de todas las páginas posteriores.
  m4a / mp4   el contenido de los átomos `mdat` (los tags viven en `moov/udta`)
  wav / aiff  el chunk de muestras (`data` / `SSND`)

Cualquier otro formato, o un fichero que no se deja parsear, se hashea entero y
se marca como id **débil** (`strong=False`): funciona, pero editar sus etiquetas
lo cambia.

El mismo audio da el mismo id esté suelto en disco o dentro de un `.tmisc`: el
contenedor solo le añade portada y metadatos alrededor.
"""

import struct
from typing import BinaryIO

from blake3 import blake3

# Versión del esquema de id. Dos nodos con esquemas distintos no se entienden
# (la misma canción tiene ids distintos en cada uno): se anuncia en
# /.well-known/tmisc.json y en el ping P2P.
ID_SCHEME = "b3-untagged-1"

CHUNK = 1 << 20


def content_id(fh: BinaryIO, offset: int = 0, size: int | None = None) -> tuple[str, bool]:
    """(id hex, ¿fuerte?) del audio que ocupa [offset, offset+size) en `fh`."""
    if size is None:
        fh.seek(0, 2)
        size = fh.tell() - offset
    end = offset + size
    fh.seek(offset)
    head = fh.read(min(size, 64))

    h = blake3()
    try:
        start = _skip_id3v2(fh, offset, end)
        fh.seek(start)
        magic = fh.read(12)

        if magic[:4] == b"fLaC":
            strong = _flac(h, fh, start, _strip_tail_tags(fh, start, end))
        elif head[:4] == b"OggS":
            strong = _ogg(h, fh, offset, end)
        elif head[4:8] == b"ftyp":
            strong = _mp4(h, fh, offset, end)
        elif head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            strong = _iff(h, fh, offset, end, b"data", big_endian=False, header=12)
        elif head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
            strong = _iff(h, fh, offset, end, b"SSND", big_endian=True, header=12)
        elif start > offset or _looks_mpeg(magic):
            _hash_range(h, fh, start, _strip_tail_tags(fh, start, end) - start)
            strong = True
        else:
            strong = False
    except (struct.error, ValueError, OSError):
        strong = False

    if not strong:
        h = blake3()
        _hash_range(h, fh, offset, size)
    return h.hexdigest(), strong


def content_id_bytes(data: bytes) -> tuple[str, bool]:
    import io
    return content_id(io.BytesIO(data), 0, len(data))


# --- utilidades -------------------------------------------------------------

def _hash_range(h, fh: BinaryIO, start: int, length: int) -> None:
    fh.seek(start)
    remaining = length
    while remaining > 0:
        chunk = fh.read(min(CHUNK, remaining))
        if not chunk:
            raise ValueError("fichero truncado")
        h.update(chunk)
        remaining -= len(chunk)


def _read_at(fh: BinaryIO, pos: int, n: int) -> bytes:
    fh.seek(pos)
    return fh.read(n)


def _looks_mpeg(b: bytes) -> bool:
    # Sync de frame MPEG audio (11 bits a 1) o ADTS (AAC crudo).
    return len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0


def _skip_id3v2(fh: BinaryIO, pos: int, end: int) -> int:
    """Salta una o varias etiquetas ID3v2 al principio (algunos FLAC también las llevan)."""
    while pos + 10 <= end:
        hdr = _read_at(fh, pos, 10)
        if hdr[:3] != b"ID3" or any(b & 0x80 for b in hdr[6:10]):
            break
        size = (hdr[6] << 21) | (hdr[7] << 14) | (hdr[8] << 7) | hdr[9]
        pos += 10 + size + (10 if hdr[5] & 0x10 else 0)   # +footer si lo declara
    return min(pos, end)


def _strip_tail_tags(fh: BinaryIO, start: int, end: int) -> int:
    """Fin del audio quitando ID3v1, APEv2 y Lyrics3v2 del final (en cualquier orden)."""
    while True:
        before = end
        if end - start >= 128 and _read_at(fh, end - 128, 3) == b"TAG":
            end -= 128
        if end - start >= 32:
            foot = _read_at(fh, end - 32, 32)
            if foot[:8] == b"APETAGEX":
                tag_size = struct.unpack("<I", foot[12:16])[0]       # incluye el footer
                flags = struct.unpack("<I", foot[20:24])[0]
                end -= tag_size + (32 if flags & 0x80000000 else 0)  # + header opcional
        if end - start >= 15:
            tail = _read_at(fh, end - 15, 15)
            if tail[6:] == b"LYRICS200" and tail[:6].isdigit():
                end -= int(tail[:6]) + 15
        if end == before or end <= start:
            return max(end, start)


# --- formatos ---------------------------------------------------------------

def _flac(h, fh: BinaryIO, start: int, end: int) -> bool:
    pos = start + 4
    while True:
        hdr = _read_at(fh, pos, 4)
        if len(hdr) < 4:
            return False
        pos += 4 + int.from_bytes(hdr[1:4], "big")
        if hdr[0] & 0x80:            # último bloque de metadatos
            break
        if pos >= end:
            return False
    if pos >= end:
        return False
    _hash_range(h, fh, pos, end - pos)
    return True


def _ogg_header_packets(first: bytes) -> int | None:
    """Cuántos paquetes de cabecera tiene el stream (no son audio)."""
    if first.startswith(b"OpusHead"):
        return 2                                   # OpusHead + OpusTags
    if first.startswith(b"\x01vorbis"):
        return 3                                   # ident + comentarios + setup
    if first.startswith(b"\x7fFLAC") and len(first) >= 9:
        return 1 + int.from_bytes(first[7:9], "big")
    if first.startswith(b"Speex   ") and len(first) >= 72:
        return 2 + struct.unpack("<I", first[68:72])[0]
    return None


def _ogg(h, fh: BinaryIO, start: int, end: int) -> bool:
    pos = start
    serial = None
    n_headers = None
    packet = 0
    first_packet = bytearray()
    hashed = False

    while pos + 27 <= end:
        hdr = _read_at(fh, pos, 27)
        if hdr[:4] != b"OggS":
            return False
        page_serial = struct.unpack("<I", hdr[14:18])[0]
        nseg = hdr[26]
        lacing = _read_at(fh, pos + 27, nseg)
        body_len = sum(lacing)
        body_pos = pos + 27 + nseg
        pos = body_pos + body_len
        if serial is None:
            serial = page_serial
        elif page_serial != serial:
            continue                    # solo el primer stream lógico

        body = _read_at(fh, body_pos, body_len)
        off = 0
        for seg in lacing:
            data = body[off:off + seg]
            off += seg
            if n_headers is None:
                first_packet += data
            elif packet >= n_headers:
                h.update(data)
                hashed = True
            if seg < 255:               # fin de paquete
                if n_headers is None:
                    n_headers = _ogg_header_packets(bytes(first_packet))
                    if n_headers is None:
                        return False
                packet += 1
    return hashed


def _mp4(h, fh: BinaryIO, start: int, end: int) -> bool:
    pos = start
    found = False
    while pos + 8 <= end:
        hdr = _read_at(fh, pos, 16)
        size = struct.unpack(">I", hdr[:4])[0]
        kind = hdr[4:8]
        header = 8
        if size == 1:
            size = struct.unpack(">Q", hdr[8:16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return False
        if kind == b"mdat":
            _hash_range(h, fh, pos + header, size - header)
            found = True
        pos += size
    return found


def _iff(h, fh: BinaryIO, start: int, end: int, want: bytes, *,
         big_endian: bool, header: int) -> bool:
    pos = start + header
    fmt = ">I" if big_endian else "<I"
    while pos + 8 <= end:
        hdr = _read_at(fh, pos, 8)
        size = struct.unpack(fmt, hdr[4:8])[0]
        if hdr[:4] == want:
            _hash_range(h, fh, pos + 8, min(size, end - pos - 8))
            return True
        pos += 8 + size + (size & 1)        # los chunks van alineados a 2 bytes
    return False
