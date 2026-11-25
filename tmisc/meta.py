"""Segmento META: JSON UTF-8 canónico.

Canónico = `sort_keys` + separadores compactos. El `meta_crc32` del footer debe
ser reproducible: si el mismo dict serializara distinto en dos ejecuciones, el
crc cambiaría sin que el contenido lo hiciera.

Campos que cargan peso (no son decorativos):

  audio.mime         -> es el Content-Type que sirve el gateway. Sin él, VLC va
                        a ciegas y el <audio> del navegador se niega a sonar.
  audio.ext          -> construye /stream/{id}.{ext}; varios clientes sniffean
                        el formato por la URL.
  audio.duration_ms  -> el catálogo y las playlists la muestran sin decodificar.
  ids.recording_mbid -> MBID de la grabación en MusicBrainz (el que escribe Picard).
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


@dataclass
class AudioMeta:
    mime: str = "application/octet-stream"
    codec: str = "bin"
    ext: str = "bin"
    size: int = 0
    duration_ms: int | None = None
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    lossless: bool | None = None


@dataclass
class TrackMeta:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    track_no: int | None = None
    disc_no: int | None = None
    year: int | None = None
    genre: list[str] = field(default_factory=list)
    isrc: str | None = None


@dataclass
class IdsMeta:
    recording_mbid: str | None = None
    release_mbid: str | None = None
    artist_mbid: str | None = None
    tidol_track_id: str | None = None


@dataclass
class CoverMeta:
    present: bool = False
    width: int | None = None
    height: int | None = None
    container: str = "bmp"
    depth: int | None = None
    source: str | None = None
    source_url: str | None = None


@dataclass
class ProvenanceMeta:
    created_at: str = ""
    encoder: str = ""
    source_filename: str | None = None


@dataclass
class TmiscMeta:
    tmisc: int = 2
    id: str = ""
    audio: AudioMeta = field(default_factory=AudioMeta)
    track: TrackMeta = field(default_factory=TrackMeta)
    ids: IdsMeta = field(default_factory=IdsMeta)
    cover: CoverMeta = field(default_factory=CoverMeta)
    provenance: ProvenanceMeta = field(default_factory=ProvenanceMeta)
    replaygain: dict = field(default_factory=dict)
    lyrics: str | None = None
    x: dict = field(default_factory=dict)

    def to_json(self) -> bytes:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> "TmiscMeta":
        d = json.loads(raw.decode("utf-8"))
        return cls(
            tmisc=d.get("tmisc", 2),
            id=d.get("id", ""),
            audio=AudioMeta(**d.get("audio", {})),
            track=TrackMeta(**d.get("track", {})),
            ids=IdsMeta(**d.get("ids", {})),
            cover=CoverMeta(**d.get("cover", {})),
            provenance=ProvenanceMeta(**d.get("provenance", {})),
            replaygain=d.get("replaygain") or {},
            lyrics=d.get("lyrics"),
            x=d.get("x") or {},
        )

    @property
    def display_title(self) -> str:
        return self.track.title or "(sin título)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
