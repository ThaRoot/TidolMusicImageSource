"""Escaneo de la biblioteca: .tmisc y ficheros de audio normales, en su sitio.

Coste por fichero ya conocido: 1 `stat` (re-escanear 10.000 pistas es ~1 s).
Coste por fichero nuevo o modificado: leer su audio UNA vez para calcular el id
(`audioid.content_id`, el BLAKE3 del audio sin etiquetas). BLAKE3 va a velocidad
de disco, así que la primera indexación de una biblioteca grande tarda lo que
tarda leerla; a partir de ahí, (tamaño, mtime) en SQLite evita repetirlo.

El hash que trae el footer de un .tmisc v2 NO se usa como id: cubre el audio CON
sus etiquetas, y entonces la misma canción suelta y empaquetada tendría dos ids.

Los ficheros de audio no se convierten ni se copian: se sirven tal cual.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Callable

from .audioid import content_id, content_id_bytes
from .catalog import Catalog
from .container import AUDIO_EXTENSIONS, probe, probe_plain, read_meta
from .errors import TmiscError
from .hashing import hash_file_range
from .meta import CoverMeta, TmiscMeta

TMISC_EXTENSIONS = (".tmisc", ".bmp")
EXTENSIONS = TMISC_EXTENSIONS + AUDIO_EXTENSIONS


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
    """Indexa UN fichero: lo usa la pasarela P2P con lo recién traído de un peer."""
    _index_one(path, catalog)


def _index_one(path: str, catalog: Catalog) -> None:
    if path.lower().endswith(TMISC_EXTENSIONS):
        _index_tmisc(path, catalog)
    else:
        _index_plain(path, catalog)


def _index_tmisc(path: str, catalog: Catalog) -> None:
    info = probe(path)
    if info.streamable:
        with open(path, "rb") as fh:
            track_id, _strong = content_id(fh, info.audio_offset, info.audio_size)
    else:
        # LSB: el audio hay que sacarlo de los píxeles antes de poder hashearlo.
        from .legacy_lsb import extract_lsb_audio
        track_id, _strong = content_id_bytes(extract_lsb_audio(info))

    catalog.upsert(
        info, track_id, read_meta(info),
        streamable="direct" if info.streamable else "cached",
        file_b3=_file_hash(path),
    )


def _index_plain(path: str, catalog: Catalog) -> None:
    from .encode import read_tags
    from .importer import find_embedded_cover, find_folder_cover

    info = probe_plain(path)
    with open(path, "rb") as fh:
        track_id, strong = content_id(fh, 0, info.file_size)

    audio, track, ids = read_tags(path)
    audio.mime, audio.ext, audio.size = info.mime, info.ext, info.file_size
    audio.codec = info.ext
    has_cover = find_embedded_cover(path) is not None or find_folder_cover(path) is not None
    if not track.title:
        track.title = os.path.splitext(os.path.basename(path))[0]
    meta = TmiscMeta(
        id=track_id, audio=audio, track=track, ids=ids,
        cover=CoverMeta(present=has_cover, container="embedded" if has_cover else ""),
        x={"id_strength": "strong" if strong else "weak"},
    )
    catalog.upsert(info, track_id, meta, streamable="direct", file_b3=_file_hash(path))


def _file_hash(path: str) -> str:
    """BLAKE3 del fichero ENTERO (no del audio): la integridad de una copia P2P.

    El id deja fuera las etiquetas a propósito, así que no basta para saber que
    un fichero llegó intacto: un byte corrupto dentro del ID3 no lo cambiaría. El
    peer anuncia este hash y quien descarga comprueba los dos. Justo después de
    `content_id` el fichero está en la caché de páginas: esta segunda lectura
    casi no toca el disco.
    """
    with open(path, "rb") as fh:
        return hash_file_range(fh, 0, os.fstat(fh.fileno()).st_size).hex()
