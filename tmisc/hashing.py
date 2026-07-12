"""BLAKE3 — el id de un track es el hash de su AUDIO, no del archivo.

Dos .tmisc con la misma canción y distinta portada tienen el mismo id, así que
el catálogo y el P2P deduplican solos.

`blake3` es dependencia dura a propósito: un fallback silencioso a blake2b
produciría ids distintos para el mismo audio y rompería ese dedupe.
"""

from blake3 import blake3

HASH_LEN = 32          # BLAKE3-256
HEX_LEN = 64


class AudioHasher:
    """Hashea en streaming, mientras los bytes se escriben o se leen."""

    def __init__(self) -> None:
        self._h = blake3()
        self.size = 0

    def update(self, chunk: bytes) -> None:
        self._h.update(chunk)
        self.size += len(chunk)

    def digest(self) -> bytes:
        return self._h.digest(length=HASH_LEN)

    def hexdigest(self) -> str:
        return self._h.digest(length=HASH_LEN).hex()


def hash_bytes(data: bytes) -> bytes:
    return blake3(data).digest(length=HASH_LEN)


def hash_file_range(fh, offset: int, length: int, chunk: int = 1 << 20) -> bytes:
    """BLAKE3 de un slice de un fichero abierto, sin cargarlo en RAM.

    Se usa para calcular el id de los .tmisc v1, que no traen hash en el trailer.
    """
    h = blake3()
    fh.seek(offset)
    remaining = length
    while remaining > 0:
        data = fh.read(min(chunk, remaining))
        if not data:
            break
        remaining -= len(data)
        h.update(data)
    return h.digest(length=HASH_LEN)
