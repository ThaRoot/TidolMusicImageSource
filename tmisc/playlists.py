"""Playlists M3U y XSPF — "abrir la cola entera en VLC".

TRAMPA: el <duration> de XSPF va en MILISEGUNDOS y el #EXTINF de M3U en
SEGUNDOS. Confundirlos hace que VLC muestre duraciones absurdas y a veces se
niegue a hacer seek. Está cubierto por los tests.

XSPF es el formato nativo de VLC y es el que conviene preferir: lleva título,
artista, álbum y carátula por pista. M3U solo lleva un texto y la duración.
"""

from xml.sax.saxutils import escape

from .catalog import CatalogTrack


def stream_url(base: str, t: CatalogTrack) -> str:
    # La extensión en la URL es opcional para nosotros, pero varios clientes
    # sniffean el formato por ella. Es gratis, y evita una clase entera de bugs.
    return f"{base}/stream/{t.id}.{t.ext}"


def cover_url(base: str, t: CatalogTrack) -> str:
    return f"{base}/cover/{t.id}.jpg"


def to_m3u(tracks: list[CatalogTrack], base: str, title: str = "Tidol") -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for t in tracks:
        seconds = int((t.duration_ms or 0) / 1000) or -1      # SEGUNDOS
        label = " - ".join(x for x in (t.artist, t.title) if x) or t.id[:12]
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(stream_url(base, t))
    return "\n".join(lines) + "\n"


def to_xspf(tracks: list[CatalogTrack], base: str, title: str = "Tidol") -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<playlist version="1" xmlns="http://xspf.org/ns/0/">',
        f"  <title>{escape(title)}</title>",
        "  <trackList>",
    ]
    for t in tracks:
        out.append("    <track>")
        out.append(f"      <location>{escape(stream_url(base, t))}</location>")
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
        out.append(f"      <image>{escape(cover_url(base, t))}</image>")
        out.append("    </track>")
    out += ["  </trackList>", "</playlist>"]
    return "\n".join(out) + "\n"
