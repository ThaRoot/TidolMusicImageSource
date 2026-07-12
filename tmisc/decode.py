"""Extracción del audio de un .tmisc.

En el camino v2/append esto es puro `seek` + `read`: Pillow no llega a
importarse. Solo el modo LSB legacy necesita decodificar píxeles. Reducir la
superficie es mejor que endurecerla.
"""

import os
import tempfile
from typing import Iterator

from . import format as fmt
from .container import TmiscInfo, iter_audio_slice, probe, read_audio
from .errors import ChecksumError
from .hashing import hash_bytes

MODE_NAMES = {
    fmt.MODE_LSB_V1: "lsb",
    fmt.MODE_APPEND_V1: "append-v1",
    fmt.MODE_APPEND_V2: "append-v2",
    fmt.MODE_LSB_V2: "lsb-v2",
}


def mode_name(info: TmiscInfo) -> str:
    return MODE_NAMES.get(info.mode, "desconocido")


def decode_file(path: str) -> tuple[TmiscInfo, Iterator[bytes]]:
    """Info + iterador de los bytes del audio, sin materializarlo."""
    info = probe(path)
    if info.streamable:
        return info, iter_audio_slice(info)
    return info, iter([read_audio(info)])


def decode_bytes(data: bytes) -> tuple[bytes, str, str]:
    """(audio, ext, modo) a partir del contenido de un .tmisc.

    Necesita un fichero porque el formato se lee por offsets desde el final;
    trabajar sobre un stream no posicionable no tendría sentido.
    """
    with tempfile.NamedTemporaryFile(suffix=".tmisc", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        info = probe(tmp_path)
        return read_audio(info), info.ext, mode_name(info)
    finally:
        os.unlink(tmp_path)


def verify(info: TmiscInfo) -> None:
    """Comprueba que el audio no ha sido alterado, contra el hash del footer."""
    if not info.audio_blake3:
        return                      # v1: no hay hash que comparar
    actual = hash_bytes(read_audio(info))
    if actual != info.audio_blake3:
        raise ChecksumError(
            f"El audio no coincide con el hash del footer "
            f"(esperado {info.audio_blake3.hex()[:16]}…, obtenido {actual.hex()[:16]}…)."
        )
