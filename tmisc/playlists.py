"""Playlists M3U y XSPF — "abrir la cola entera en VLC".

TRAMPA: el <duration> de XSPF va en MILISEGUNDOS y el #EXTINF de M3U en
SEGUNDOS. Confundirlos hace que VLC muestre duraciones absurdas y a veces se
niegue a hacer seek. Está cubierto por los tests.

XSPF es el formato nativo de VLC y es el que conviene preferir: lleva título,
artista, álbum y carátula por pista. M3U solo lleva un texto y la duración.
"""

from urllib.parse import quote
from xml.sax.saxutils import escape

from .catalog import CatalogTrack


def _auth(token: str | None) -> str:
    """El token va en la query, no en una cabecera.

    VLC no manda cabeceras propias al seguir los <location> de una playlist. Si
    las URLs de dentro no llevan el `?k=`, el reproductor baja la lista y luego
    se come un 401 en CADA pista: la playlist parece funcionar y no suena nada.
    """
    return f"?k={quote(token, safe='')}" if token else ""


def stream_url(base: str, t: CatalogTrack, token: str | None = None) -> str:
    # La extensión en la URL es opcional para nosotros, pero varios clientes
    # sniffean el formato por ella. Es gratis, y evita una clase entera de bugs.
    return f"{base}/stream/{t.id}.{t.ext}{_auth(token)}"


def cover_url(base: str, t: CatalogTrack, token: str | None = None) -> str:
    return f"{base}/cover/{t.id}.jpg{_auth(token)}"


def to_m3u(tracks: list[CatalogTrack], base: str, title: str = "Tidol",
           token: str | None = None) -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for t in tracks:
        seconds = int((t.duration_ms or 0) / 1000) or -1      # SEGUNDOS
        label = " - ".join(x for x in (t.artist, t.title) if x) or t.id[:12]
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(stream_url(base, t, token))
    return "\n".join(lines) + "\n"


def to_xspf(tracks: list[CatalogTrack], base: str, title: str = "Tidol",
            token: str | None = None) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<playlist version="1" xmlns="http://xspf.org/ns/0/">',
        f"  <title>{escape(title)}</title>",
        "  <trackList>",
    ]
    for t in tracks:
        out.append("    <track>")
        out.append(f"      <location>{escape(stream_url(base, t, token))}</location>")
        if t.title:
            out.append(f"      <title>{escape(t.title)}</title>")
        if t.artist:
            out.append(f"      <creator>{escape(t.artist)}</creator>")
        if t.album:
            out.append(f"      <album>{escape(t.album)}</album>")
        if t.track_no:
            out.append(f"      <trackNum>{t.track_no}</trackNum>")
        if t.duration_ms:
            out.append(f"      <duration>{int(t.duration_ms)}</duration>")   # MILISEGUNDOS
        out.append(f"      <image>{escape(cover_url(base, t, token))}</image>")
        out.append("    </track>")
    out += ["  </trackList>", "</playlist>"]
    return "\n".join(out) + "\n"
