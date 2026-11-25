"""Escritura de .tmisc v2 — en streaming, hasheando al vuelo.

El audio nunca se materializa entero en memoria: se copia por chunks del origen
al destino mientras BLAKE3 y CRC32 se actualizan sobre la marcha. Codificar un
FLAC de 400 MB usa la misma RAM que uno de 4 MB.
"""

import io
import os
import zlib
from typing import BinaryIO

from . import format as fmt
from .cover import DEFAULT_SIDE, generate_identicon, load_cover, prepare_cover
from .errors import TmiscError
from .hashing import AudioHasher, hash_bytes
from .meta import AudioMeta, CoverMeta, IdsMeta, ProvenanceMeta, TmiscMeta, TrackMeta, now_iso
from .sniff import sniff

ENCODER = "tmisc/2.0.0"
COPY_CHUNK = 1 << 20


def probe_audio_tags(path: str) -> tuple[AudioMeta, TrackMeta]:
    """Duración, bitrate y tags del audio original, vía mutagen. Todo opcional."""
    audio, track, _ids = read_tags(path)
    return audio, track


def read_tags(path: str) -> tuple[AudioMeta, TrackMeta, IdsMeta]:
    """Como probe_audio_tags, más los MBID de MusicBrainz (los que escribe Picard)."""
    audio, track, ids = AudioMeta(), TrackMeta(), IdsMeta()
    try:
        import mutagen
        mf = mutagen.File(path, easy=True)
    except Exception:
        return audio, track, ids
    if mf is None:
        return audio, track, ids

    info = getattr(mf, "info", None)
    if info is not None:
        if getattr(info, "length", None):
            audio.duration_ms = int(info.length * 1000)
        if getattr(info, "bitrate", None):
            audio.bitrate_kbps = int(info.bitrate / 1000)
        audio.sample_rate = getattr(info, "sample_rate", None)
        audio.channels = getattr(info, "channels", None)

    def first(key: str) -> str | None:
        v = mf.get(key)
        return str(v[0]) if v else None

    track.title = first("title")
    track.artist = first("artist")
    track.album = first("album")
    track.album_artist = first("albumartist")
    for key, attr in (("tracknumber", "track_no"), ("discnumber", "disc_no")):
        raw = first(key)
        if raw:
            try:
                setattr(track, attr, int(raw.split("/")[0]))
            except ValueError:
                pass
    date = first("date")
    if date and date[:4].isdigit():
        track.year = int(date[:4])
    genre = first("genre")
    if genre:
        track.genre = [genre]
    track.isrc = first("isrc")
    # Claves "easy" de mutagen: valen igual para ID3 (UFID/TXXX), Vorbis comments y MP4.
    for key, attr in (("musicbrainz_trackid", "recording_mbid"),
                      ("musicbrainz_albumid", "release_mbid"),
                      ("musicbrainz_artistid", "artist_mbid")):
        try:
            setattr(ids, attr, first(key))
        except Exception:
            pass
    return audio, track, ids


def encode_to_file(
    audio_fh: BinaryIO,
    audio_size: int,
    out_path: str,
    *,
    cover_data: bytes | None = None,
    audio_meta: AudioMeta | None = None,
    track: TrackMeta | None = None,
    ids: IdsMeta | None = None,
    source_filename: str | None = None,
    side: int = DEFAULT_SIDE,
    depth: int = 24,
    container: str = "bmp",
    lyrics: str | None = None,
) -> fmt.Footer:
    """Escribe un .tmisc v2 en `out_path` y devuelve su footer."""
    if audio_size <= 0:
        raise TmiscError("El audio está vacío.")

    start = audio_fh.tell()
    head = audio_fh.read(16)
    audio_fh.seek(start)
    codec, mime, ext = sniff(head)

    # Sin portada aportada: se genera un identicon a partir del hash del audio.
    # Requiere una pasada previa de lectura, que con BLAKE3 es prácticamente gratis.
    if cover_data is None:
        pre = AudioHasher()
        _consume(audio_fh, audio_size, pre.update)
        audio_fh.seek(start)
        cover_img = generate_identicon(pre.digest(), side=side)
    else:
        cover_img = load_cover(cover_data)

    cover_bytes, cw, ch, cdepth, container_id = prepare_cover(
        cover_img, side=side, depth=depth, container=container
    )

    am = audio_meta or AudioMeta()
    am.codec, am.mime, am.ext, am.size = codec, mime, ext, audio_size
    if am.lossless is None:
        am.lossless = codec in ("flac", "wav", "aiff", "wavpack", "ape")

    tmp = out_path + ".part"
    hasher = AudioHasher()
    crc = 0

    try:
        with open(tmp, "wb") as out:
            out.write(cover_bytes)
            audio_offset = out.tell()

            def sink(chunk: bytes) -> None:
                nonlocal crc
                hasher.update(chunk)
                crc = zlib.crc32(chunk, crc)
                out.write(chunk)

            written = _consume(audio_fh, audio_size, sink)
            if written != audio_size:
                raise TmiscError(f"Se esperaban {audio_size} B de audio y llegaron {written}.")

            meta = TmiscMeta(
                id=hasher.hexdigest(),
                audio=am,
                track=track or TrackMeta(),
                ids=ids or IdsMeta(),
                cover=CoverMeta(
                    present=True, width=cw, height=ch,
                    container=container.lower(), depth=cdepth,
                    source="provided" if cover_data else "identicon",
                ),
                provenance=ProvenanceMeta(
                    created_at=now_iso(), encoder=ENCODER,
                    source_filename=source_filename,
                ),
                lyrics=lyrics,
            )
            meta_json = meta.to_json()

            meta_offset = out.tell()
            out.write(meta_json)

            footer = fmt.Footer(
                mode=fmt.MODE_APPEND_V2,
                flags=fmt.FLAG_HAS_META,
                container=container_id,
                file_size=meta_offset + len(meta_json) + fmt.FOOTER_SIZE,
                image_size=len(cover_bytes),
                audio_offset=audio_offset,
                audio_size=audio_size,
                meta_offset=meta_offset,
                meta_size=len(meta_json),
                audio_crc32=crc & 0xFFFFFFFF,
                audio_blake3=hasher.digest(),
                meta_crc32=fmt.crc32(meta_json),
            )
            out.write(footer.pack())

        # Rename atómico: un .tmisc a medio escribir nunca es visible para el
        # scanner, que si no lo indexaría truncado.
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    return footer


def encode_bytes(audio: bytes, *, cover_data: bytes | None = None, **kw) -> bytes:
    """Variante en memoria, para el endpoint /encode. Sujeta al límite de subida."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.tmisc")
        encode_to_file(io.BytesIO(audio), len(audio), out, cover_data=cover_data, **kw)
        with open(out, "rb") as fh:
            return fh.read()


def _consume(fh: BinaryIO, size: int, sink) -> int:
    remaining, total = size, 0
    while remaining > 0:
        chunk = fh.read(min(COPY_CHUNK, remaining))
        if not chunk:
            break
        sink(chunk)
        remaining -= len(chunk)
        total += len(chunk)
    return total


def cover_hash(cover_bytes: bytes) -> str:
    return hash_bytes(cover_bytes).hex()
