"""Escaneo de la biblioteca — barato por diseño.

Coste por fichero ya conocido: 1 `stat`.
Coste por fichero nuevo:       ~3 syscalls y menos de 64 KB leídos (footer + JSON).

Nunca se abre Pillow y nunca se lee el audio. Por eso una biblioteca de 10.000
pistas y 200 GB se re-escanea en cosa de un segundo.

Excepciones: los .tmisc v1 no traen hash en el trailer, así que hay que leer su
audio una vez para calcular el BLAKE3; y los LSB hay que decodificarlos. Ambas
cosas se hacen UNA vez y quedan cacheadas en SQLite.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Callable

from .catalog import Catalog
from .container import probe, read_meta
from .errors import TmiscError
from .hashing import hash_file_range

EXTENSIONS = (".tmisc", ".bmp")


@dataclass
class ScanStats:
    scanned: int = 0
    added: int = 0
    skipped: int = 0
    removed: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned, "added": self.added, "skipped": self.skipped,
            "removed": self.removed, "failed": self.failed, "errors": self.errors[:20],
        }


def iter_candidates(root: str):
    """Recorrido recursivo, saltándose los ocultos y los .part a medio escribir."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
            elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(EXTENSIONS):
                yield e


def scan(roots: str | Sequence[str], catalog: Catalog,
         progress: Callable[[ScanStats], None] | None = None) -> ScanStats:
    """Reindexa el catálogo a partir de una o varias raíces.

    Varias raíces, y no una llamada por raíz, porque al final se olvida todo path
    conocido que no se haya visto: dos pasadas independientes harían que la
    segunda desindexase lo que encontró la primera.
    """
    if isinstance(roots, str):
        roots = [roots]

    stats = ScanStats()
    known = catalog.known_files()
    seen: set[str] = set()

    for entry in (e for root in roots for e in iter_candidates(root)):
        path = entry.path
        if path in seen:
            continue
        seen.add(path)
        stats.scanned += 1

        try:
            st = entry.stat()
        except OSError:
            continue

        prev = known.get(path)
        if prev == (st.st_size, st.st_mtime_ns):
            stats.skipped += 1
            continue

        try:
            _index_one(path, catalog)
            stats.added += 1
        except TmiscError as e:
            # Un fichero que aún se está copiando falla aquí (el footer declara
            # un file_size que todavía no coincide). Se reintentará en el
            # siguiente escaneo, cuando ya esté completo.
            stats.failed += 1
            stats.errors.append(f"{os.path.basename(path)}: {e}")
        except OSError as e:
            stats.failed += 1
            stats.errors.append(f"{os.path.basename(path)}: {e}")

        if progress and stats.scanned % 50 == 0:
            progress(stats)

    for path in known:
        if path not in seen:
            catalog.forget_path(path)
            stats.removed += 1

    if progress:
        progress(stats)
    return stats


def index_file(path: str, catalog: Catalog) -> None:
    """Indexa UN fichero: lo usa la pasarela P2P con los .tmisc recién traídos."""
    _index_one(path, catalog)


def _index_one(path: str, catalog: Catalog) -> None:
    info = probe(path)

    if info.audio_blake3:
        track_id = info.audio_blake3.hex()
    elif info.streamable:
        # v1 append: no trae hash. Se calcula leyendo el audio una sola vez.
        with open(path, "rb") as fh:
            track_id = hash_file_range(fh, info.audio_offset, info.audio_size).hex()
    else:
        # LSB: el audio hay que sacarlo de los píxeles antes de poder hashearlo.
        from .hashing import hash_bytes
        from .legacy_lsb import extract_lsb_audio
        track_id = hash_bytes(extract_lsb_audio(info)).hex()

    catalog.upsert(
        info, track_id, read_meta(info),
        streamable="direct" if info.streamable else "cached",
    )
