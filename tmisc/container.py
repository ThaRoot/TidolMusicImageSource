"""Lectura de un .tmisc — todo por `seek`, nunca cargando el archivo en RAM.

`probe()` identifica un fichero leyendo como mucho 120 bytes del final. Eso es
lo que hace barato indexar una biblioteca de 10.000 pistas (y lo que permite que
el gateway sirva un rango arbitrario del audio sin decodificar nada).

Compatibilidad hacia atrás sin migrar nada: un .tmisc v1 en modo append ya tiene
el offset del audio derivable (`size - 27 - audio_size`), así que se sirve con
Range exactamente igual que un v2. Lo único que le falta es el hash (se calcula
una vez al indexar) y los metadatos.
"""

import os
import struct
from dataclasses import dataclass
from typing import Iterator

from . import format as fmt
from .errors import CorruptFooterError, NotTmiscError
from .meta import TmiscMeta
from .sniff import mime_for_ext, safe_ext, sniff

CHUNK = 256 * 1024  # 256 KB: equilibrio entre syscalls y latencia de arranque


@dataclass
class TmiscInfo:
    """Todo lo necesario para servir el fichero, sin volver a abrirlo."""

    path: str
    file_size: int
    version: int              # 1 | 2
    mode: int
    audio_offset: int
    audio_size: int
    image_size: int
    ext: str
    mime: str
    meta_offset: int = 0
    meta_size: int = 0
    audio_blake3: bytes | None = None   # None en v1: se calcula al indexar
    audio_crc32: int = 0
    container: int = fmt.CONTAINER_BMP
    mtime_ns: int = 0

    @property
    def is_lsb(self) -> bool:
        return self.mode in (fmt.MODE_LSB_V1, fmt.MODE_LSB_V2)

    @property
    def streamable(self) -> bool:
        """¿El audio es un slice contiguo del fichero?

        En LSB el audio vive repartido por los bits de los píxeles: hay que
        decodificar la imagen entera antes de poder servir un solo byte.
        """
        return not self.is_lsb

    @property
    def track_id(self) -> str | None:
        return self.audio_blake3.hex() if self.audio_blake3 else None

    @property
    def image_mime(self) -> str:
        return fmt.CONTAINER_MIME.get(self.container, "image/bmp")


def probe(path: str) -> TmiscInfo:
    """Identifica un .tmisc leyendo solo su final. 2-3 syscalls, <256 B."""
    st = os.stat(path)
    size = st.st_size
    if size < fmt.MIN_IMAGE_BYTES:
        raise NotTmiscError("Archivo demasiado pequeño.")

    with open(path, "rb") as fh:
        head = fh.read(14)
        image_size = fmt.bmp_declared_size(head) or 0

        # --- 1) ¿footer v2? Los últimos 8 bytes son el tail auto-localizable ---
        if size >= fmt.FOOTER_SIZE:
            fh.seek(-fmt.FOOTER_TAIL_SIZE, os.SEEK_END)
            magic, footer_size = struct.unpack(fmt.FOOTER_TAIL_FMT, fh.read(fmt.FOOTER_TAIL_SIZE))
            if magic == fmt.MAGIC_V2:
                if footer_size < fmt.FOOTER_SIZE or footer_size > size:
                    raise CorruptFooterError("footer_size fuera de rango.")
                fh.seek(-footer_size, os.SEEK_END)
                footer = fmt.Footer.unpack(fh.read(footer_size))
                footer.validate_against(size)
                return _from_footer(path, footer, size, st.st_mtime_ns)

        # --- 2) ¿trailer v1 append? ---
        if size >= fmt.TRAILER_V1_SIZE:
            fh.seek(-fmt.TRAILER_V1_SIZE, os.SEEK_END)
            raw = fh.read(fmt.TRAILER_V1_SIZE)
            magic, mode, fmt_field, audio_size, crc = struct.unpack(fmt.TRAILER_V1_FMT, raw)
            if magic == fmt.MAGIC_V1 and mode == fmt.MODE_APPEND_V1:
                audio_offset = size - fmt.TRAILER_V1_SIZE - audio_size
                # v1 no valida esto: un trailer manipulado podía hacer que el
                # "audio" empezara dentro del BMP, o antes del archivo.
                if audio_offset < fmt.MIN_IMAGE_BYTES:
                    raise CorruptFooterError("Trailer v1 apunta dentro de la imagen.")
                ext = safe_ext(fmt_field.split(b"\x00")[0].decode("ascii", "ignore"))
                # El mime se decide por los magic bytes del audio real, no por
                # el campo `fmt` del trailer (que viene de un nombre de fichero).
                fh.seek(audio_offset)
                codec, mime, sniffed_ext = sniff(fh.read(16))
                return TmiscInfo(
                    path=path, file_size=size, version=1, mode=fmt.MODE_APPEND_V1,
                    audio_offset=audio_offset, audio_size=audio_size,
                    image_size=image_size or audio_offset,
                    ext=sniffed_ext if sniffed_ext != "bin" else ext,
                    mime=mime if mime != "application/octet-stream" else mime_for_ext(ext),
                    audio_crc32=crc, mtime_ns=st.st_mtime_ns,
                )

    # --- 3) ¿LSB? Detectarlo exige decodificar píxeles: se delega a legacy_lsb ---
    from .legacy_lsb import probe_lsb
    lsb = probe_lsb(path)
    if lsb is None:
        raise NotTmiscError("El archivo no contiene audio TMISC.")
    return lsb


def _from_footer(path: str, footer: fmt.Footer, size: int, mtime_ns: int) -> TmiscInfo:
    ext, mime = "bin", "application/octet-stream"
    if footer.has_meta:
        try:
            meta = read_meta_at(path, footer.meta_offset, footer.meta_size)
            ext, mime = meta.audio.ext, meta.audio.mime
        except Exception:
            pass  # los metadatos son opcionales; el audio se sirve igual
    if mime == "application/octet-stream" and footer.streamable:
        with open(path, "rb") as fh:
            fh.seek(footer.audio_offset)
            _, mime, ext = sniff(fh.read(16))

    return TmiscInfo(
        path=path, file_size=size, version=2, mode=footer.mode,
        audio_offset=footer.audio_offset, audio_size=footer.audio_size,
        image_size=footer.image_size,
        ext=ext, mime=mime,
        meta_offset=footer.meta_offset, meta_size=footer.meta_size,
        audio_blake3=footer.audio_blake3, audio_crc32=footer.audio_crc32,
        container=footer.container, mtime_ns=mtime_ns,
    )


AUDIO_EXTENSIONS = (".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".m4b",
                    ".aac", ".wav", ".aif", ".aiff", ".wv", ".ape")


def probe_plain(path: str) -> TmiscInfo:
    """Un fichero de audio normal, indexado en su sitio: el "audio" es el fichero entero.

    Se sirve tal cual (con sus tags y su carátula embebida), así que se reproduce
    en cualquier cliente. El id NO sale de aquí: ver `audioid.content_id`.
    """
    st = os.stat(path)
    with open(path, "rb") as fh:
        head = fh.read(64)
    codec, mime, ext = sniff(head)
    if codec == "bin":
        raise NotTmiscError("No es un formato de audio reconocible.")
    return TmiscInfo(
        path=path, file_size=st.st_size, version=0, mode=fmt.MODE_PLAIN,
        audio_offset=0, audio_size=st.st_size, image_size=0,
        ext=ext, mime=mime, container=0, mtime_ns=st.st_mtime_ns,
    )


def read_meta_at(path: str, offset: int, length: int) -> TmiscMeta:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return TmiscMeta.from_json(fh.read(length))


def read_meta(info: TmiscInfo) -> TmiscMeta | None:
    if not info.meta_size:
        return None
    try:
        return read_meta_at(info.path, info.meta_offset, info.meta_size)
    except Exception:
        return None


def iter_audio_slice(info: TmiscInfo, start: int = 0, length: int | None = None,
                     chunk: int = CHUNK) -> Iterator[bytes]:
    """El generador que alimenta /stream. RSS pico = `chunk`, sea el FLAC de 4 MB o de 400 MB.

    `start` es relativo al AUDIO, no al archivo: el cliente nunca ve el contenedor.
    """
    if not info.streamable:
        raise NotTmiscError("Este .tmisc guarda el audio en los píxeles (LSB): no es un slice.")

    if length is None:
        length = info.audio_size - start
    length = max(0, min(length, info.audio_size - start))

    with open(info.path, "rb", buffering=0) as fh:
        fh.seek(info.audio_offset + start)      # <-- el seek real. O(1).
        remaining = length
        while remaining > 0:
            data = fh.read(min(chunk, remaining))
            if not data:
                break        # el fichero se truncó bajo nuestros pies: cortar limpio
            remaining -= len(data)
            yield data


def read_audio(info: TmiscInfo) -> bytes:
    """Audio completo en memoria. Solo para la CLI; el servidor usa iter_audio_slice."""
    if not info.streamable:
        from .legacy_lsb import extract_lsb_audio
        return extract_lsb_audio(info)
    return b"".join(iter_audio_slice(info))


def read_cover_bytes(info: TmiscInfo) -> bytes:
    """Los bytes de la imagen: el slice [0, image_size). Sin abrir Pillow."""
    with open(info.path, "rb") as fh:
        return fh.read(info.image_size or info.audio_offset)
