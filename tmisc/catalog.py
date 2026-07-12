"""Catálogo local en SQLite.

Dos tablas, porque el direccionamiento por contenido implica N ficheros → 1 track
lógico: la misma canción con dos portadas distintas es UN track y DOS ficheros.

  tracks  clave = blake3 del audio  -> qué canción es
  files   clave = ruta              -> dónde está y en qué offset empieza el audio

`catalog.db` es 100% derivable de los ficheros: borrarlo siempre es seguro, se
reconstruye escaneando. Los .tmisc son la única fuente de verdad. Por eso
compartir uno por Telegram o por USB "simplemente funciona": el que lo recibe lo
deja caer en su carpeta y su nodo lo indexa.
"""

import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

from .container import TmiscInfo
from .meta import TmiscMeta

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS tracks (
  id             TEXT PRIMARY KEY,     -- blake3 del AUDIO, hex
  audio_size     INTEGER NOT NULL,
  mime           TEXT NOT NULL,
  ext            TEXT NOT NULL,
  duration_ms    INTEGER,
  title          TEXT,
  artist         TEXT,
  album          TEXT,
  album_artist   TEXT,
  track_no       INTEGER,
  disc_no        INTEGER,
  year           INTEGER,
  recording_mbid TEXT,
  release_mbid   TEXT,
  artist_mbid    TEXT,
  meta_json      TEXT,
  streamable     TEXT NOT NULL DEFAULT 'direct',   -- direct | cached | broken
  indexed_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  path           TEXT PRIMARY KEY,
  id             TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  file_size      INTEGER NOT NULL,
  mtime_ns       INTEGER NOT NULL,
  container_ver  INTEGER NOT NULL,     -- 1 | 2
  mode           INTEGER NOT NULL,
  image_size     INTEGER NOT NULL,
  audio_offset   INTEGER NOT NULL,     -- <- el seek O(1) sale de aquí
  audio_size     INTEGER NOT NULL,
  preferred      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_files_id     ON files(id);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album);
CREATE INDEX IF NOT EXISTS idx_tracks_art   ON tracks(artist);
"""

# FTS5 con sus propias copias, no `content='tracks'` (external content). Una
# tabla FTS5 de contenido externo no admite DELETE directo: hay que emitir el
# comando especial 'delete' con los valores ANTIGUOS de la fila, y si no, el
# índice se corrompe ("database disk image is malformed"). Duplicar tres
# columnas de texto cuesta unos pocos KB y elimina toda esa clase de problema.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts
USING fts5(id UNINDEXED, title, artist, album);
"""


@dataclass
class CatalogTrack:
    id: str
    title: str | None
    artist: str | None
    album: str | None
    duration_ms: int | None
    mime: str
    ext: str
    audio_size: int
    streamable: str
    year: int | None = None
    track_no: int | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "mime": self.mime,
            "ext": self.ext,
            "size": self.audio_size,
            "streamable": self.streamable,
            "year": self.year,
            "track_no": self.track_no,
        }


class Catalog:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:
            self.db.executescript(FTS_SCHEMA)
            self.has_fts = True
        except sqlite3.OperationalError:
            self.has_fts = False        # SQLite compilado sin FTS5: se usa LIKE
        self.db.commit()

    # --- escritura ---------------------------------------------------------
    def upsert(self, info: TmiscInfo, track_id: str, meta: TmiscMeta | None,
               streamable: str = "direct") -> None:
        t = meta.track if meta else None
        a = meta.audio if meta else None
        ids = meta.ids if meta else None

        self.db.execute(
            """INSERT INTO tracks (id, audio_size, mime, ext, duration_ms, title, artist,
                   album, album_artist, track_no, disc_no, year,
                   recording_mbid, release_mbid, artist_mbid, meta_json, streamable, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   duration_ms=COALESCE(excluded.duration_ms, tracks.duration_ms),
                   title=COALESCE(excluded.title, tracks.title),
                   artist=COALESCE(excluded.artist, tracks.artist),
                   album=COALESCE(excluded.album, tracks.album),
                   meta_json=COALESCE(excluded.meta_json, tracks.meta_json),
                   streamable=excluded.streamable""",
            (
                track_id, info.audio_size, info.mime, info.ext,
                a.duration_ms if a else None,
                t.title if t else None, t.artist if t else None,
                t.album if t else None, t.album_artist if t else None,
                t.track_no if t else None, t.disc_no if t else None, t.year if t else None,
                ids.recording_mbid if ids else None,
                ids.release_mbid if ids else None,
                ids.artist_mbid if ids else None,
                meta.to_json().decode() if meta else None,
                streamable, int(time.time()),
            ),
        )
        self.db.execute(
            """INSERT INTO files (path, id, file_size, mtime_ns, container_ver, mode,
                   image_size, audio_offset, audio_size)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                   id=excluded.id, file_size=excluded.file_size, mtime_ns=excluded.mtime_ns,
                   container_ver=excluded.container_ver, mode=excluded.mode,
                   image_size=excluded.image_size, audio_offset=excluded.audio_offset,
                   audio_size=excluded.audio_size""",
            (
                info.path, track_id, info.file_size, info.mtime_ns,
                info.version, info.mode, info.image_size,
                info.audio_offset, info.audio_size,
            ),
        )
        if self.has_fts:
            self.db.execute("DELETE FROM tracks_fts WHERE id=?", (track_id,))
            self.db.execute(
                """INSERT INTO tracks_fts (id, title, artist, album)
                   SELECT id, title, artist, album FROM tracks WHERE id=?""",
                (track_id,),
            )
        self.db.commit()

    def forget_path(self, path: str) -> None:
        self.db.execute("DELETE FROM files WHERE path=?", (path,))
        # Un track sin ningún fichero detrás deja de existir.
        self.db.execute("DELETE FROM tracks WHERE id NOT IN (SELECT DISTINCT id FROM files)")
        if self.has_fts:
            self.db.execute("DELETE FROM tracks_fts WHERE id NOT IN (SELECT id FROM tracks)")
        self.db.commit()

    def known_files(self) -> dict[str, tuple[int, int]]:
        """{ruta: (tamaño, mtime_ns)} — para saltarse lo ya indexado con un solo stat."""
        return {
            r["path"]: (r["file_size"], r["mtime_ns"])
            for r in self.db.execute("SELECT path, file_size, mtime_ns FROM files")
        }

    # --- lectura -----------------------------------------------------------
    def expand_id(self, ident: str) -> str | None:
        """Prefijo hexadecimal -> id completo. None si no existe o es ambiguo.

        IDENT_RE acepta de 8 a 64 hex precisamente para que un humano pueda
        teclear un id corto. Un prefijo ambiguo NO resuelve: servir "el primero"
        de dos tracks distintos sería reproducir en silencio la canción
        equivocada, que es peor que un 404.
        """
        if len(ident) == 64:
            row = self.db.execute("SELECT id FROM tracks WHERE id=?", (ident,)).fetchone()
            return row["id"] if row else None
        # El ident viene validado como [0-9a-f]+, así que no puede colar un
        # metacaracter de LIKE ('%' o '_').
        rows = self.db.execute(
            "SELECT id FROM tracks WHERE id LIKE ? LIMIT 2", (ident + "%",)
        ).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None

    def resolve(self, track_id: str) -> sqlite3.Row | None:
        """Ruta y offsets del mejor fichero que contiene este track.

        Acepta prefijos. `row["id"]` es siempre el id completo: los llamantes
        deben usarlo como id canónico a partir de aquí (ETags, verificación del
        BLAKE3 al traer de un peer, nombres en el caché...).
        """
        full = self.expand_id(track_id)
        if full is None:
            return None
        return self.db.execute(
            """SELECT f.*, t.mime, t.ext, t.title, t.artist, t.album, t.streamable
               FROM files f JOIN tracks t ON t.id = f.id
               WHERE f.id = ?
               ORDER BY f.preferred DESC, f.container_ver DESC
               LIMIT 1""",
            (full,),
        ).fetchone()

    def get_meta_json(self, track_id: str) -> str | None:
        row = self.db.execute("SELECT meta_json FROM tracks WHERE id=?", (track_id,)).fetchone()
        return row["meta_json"] if row else None

    def search(self, q: str | None = None, artist: str | None = None,
               album: str | None = None, sort: str = "artist",
               limit: int = 100, offset: int = 0) -> tuple[int, list[CatalogTrack]]:
        where, params = [], []

        if q:
            if self.has_fts:
                where.append("t.id IN (SELECT id FROM tracks_fts WHERE tracks_fts MATCH ?)")
                params.append(_fts_query(q))
            else:
                where.append("(t.title LIKE ? OR t.artist LIKE ? OR t.album LIKE ?)")
                params += [f"%{q}%"] * 3
        if artist:
            where.append("t.artist = ?")
            params.append(artist)
        if album:
            where.append("t.album = ?")
            params.append(album)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        order = {
            "artist": "t.artist, t.album, t.disc_no, t.track_no",
            "title": "t.title",
            "album": "t.album, t.disc_no, t.track_no",
            "recent": "t.indexed_at DESC",
        }.get(sort, "t.artist, t.album, t.track_no")

        total = self.db.execute(
            f"SELECT COUNT(*) AS n FROM tracks t {clause}", params
        ).fetchone()["n"]

        rows = self.db.execute(
            f"SELECT t.* FROM tracks t {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

        return total, [_row_to_track(r) for r in rows]

    def facet(self, column: str) -> list[dict]:
        if column not in ("artist", "album"):
            raise ValueError(column)
        rows = self.db.execute(
            f"""SELECT {column} AS name, COUNT(*) AS tracks FROM tracks
                WHERE {column} IS NOT NULL AND {column} != ''
                GROUP BY {column} ORDER BY name"""
        ).fetchall()
        return [{"name": r["name"], "tracks": r["tracks"]} for r in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]

    def all_ids(self) -> Iterable[str]:
        for r in self.db.execute("SELECT id FROM tracks"):
            yield r["id"]

    def close(self) -> None:
        self.db.close()


def _row_to_track(r: sqlite3.Row) -> CatalogTrack:
    return CatalogTrack(
        id=r["id"], title=r["title"], artist=r["artist"], album=r["album"],
        duration_ms=r["duration_ms"], mime=r["mime"], ext=r["ext"],
        audio_size=r["audio_size"], streamable=r["streamable"],
        year=r["year"], track_no=r["track_no"],
    )


def _fts_query(q: str) -> str:
    """Prefijo por término, y comillas para que la puntuación no rompa la sintaxis FTS."""
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in terms) or '""'
