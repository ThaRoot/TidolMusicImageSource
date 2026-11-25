"""Importador: audio suelto ya presente en disco → .tmisc.

NO descarga nada: escanea lo que ya está en el disco y crea una COPIA en .tmisc.
Es opcional — el nodo sirve la biblioteca tal cual sin convertirla (scanner.py).

La portada se busca, por orden: la embebida en los tags, un cover.jpg/folder.jpg
junto al fichero, y si no hay ninguna, un identicon derivado del hash del audio.
"""

import os

from .config import Settings
from .encode import encode_to_file, probe_audio_tags
from .errors import TmiscError

AUDIO_EXTS = (".mp3", ".flac", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".m4b",
              ".aac", ".wv", ".aiff", ".webm")

COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg",
               "folder.jpeg", "front.jpg", "album.jpg")


def find_embedded_cover(path: str) -> bytes | None:
    try:
        import mutagen
        mf = mutagen.File(path)
    except Exception:
        return None
    if mf is None:
        return None

    # ID3 (mp3): frames APIC
    tags = getattr(mf, "tags", None)
    if tags is not None:
        for key in tags.keys():
            if str(key).startswith("APIC"):
                try:
                    return tags[key].data
                except Exception:
                    pass

    # FLAC / OggOpus: bloques PICTURE
    pics = getattr(mf, "pictures", None)
    if pics:
        return pics[0].data

    # MP4 / M4A: átomo covr
    if tags is not None and "covr" in tags:
        try:
            return bytes(tags["covr"][0])
        except Exception:
            pass
    return None


def find_folder_cover(path: str) -> bytes | None:
    folder = os.path.dirname(path)
    for name in COVER_NAMES:
        candidate = os.path.join(folder, name)
        if os.path.exists(candidate):
            try:
                with open(candidate, "rb") as fh:
                    return fh.read()
            except OSError:
                pass
    return None


def _safe_component(value: str | None, fallback: str) -> str:
    raw = (value or fallback).strip() or fallback
    cleaned = "".join(c for c in raw if c.isalnum() or c in " -_.,()&'").strip()
    return (cleaned or fallback)[:80]


def import_file(src: str, settings: Settings) -> str:
    """Empaqueta un audio suelto y devuelve la ruta del .tmisc resultante."""
    audio_meta, track = probe_audio_tags(src)
    cover = find_embedded_cover(src) or find_folder_cover(src)

    artist = _safe_component(track.album_artist or track.artist, "Desconocido")
    album = _safe_component(track.album, "Sin álbum")
    stem = os.path.splitext(os.path.basename(src))[0]
    name = _safe_component(track.title, stem)
    if track.track_no:
        name = f"{track.track_no:02d} {name}"

    out_dir = os.path.join(settings.library, artist, album)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{name}.tmisc")

    size = os.path.getsize(src)
    with open(src, "rb") as fh:
        encode_to_file(
            fh, size, out_path,
            cover_data=cover,
            audio_meta=audio_meta,
            track=track,
            source_filename=os.path.basename(src),
            side=settings.cover_side,
            depth=settings.cover_depth,
            container=settings.cover_container,
        )
    return out_path


def import_tree(src: str, settings: Settings, recursive: bool = True) -> dict:
    if not os.path.exists(src):
        raise TmiscError(f"No existe: {src}")

    targets: list[str] = []
    if os.path.isfile(src):
        targets = [src]
    elif recursive:
        for root, _dirs, files in os.walk(src):
            targets += [os.path.join(root, f) for f in files
                        if f.lower().endswith(AUDIO_EXTS)]
    else:
        targets = [os.path.join(src, f) for f in os.listdir(src)
                   if f.lower().endswith(AUDIO_EXTS)]

    imported, failed, errors = 0, 0, []
    for path in sorted(targets):
        try:
            import_file(path, settings)
            imported += 1
        except Exception as e:
            failed += 1
            errors.append(f"{os.path.basename(path)}: {e}")

    return {"found": len(targets), "imported": imported,
            "failed": failed, "errors": errors[:20]}
