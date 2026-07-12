"""Detección del formato de audio por MAGIC BYTES, nunca por el nombre del archivo.

Dos razones:

1. El `Content-Type` del gateway sale de aquí. Sin un mime correcto, VLC va a
   ciegas y el <audio> del navegador se niega a reproducir.
2. Seguridad: en v1 el campo `fmt` se derivaba del nombre del fichero subido y
   acababa dentro de una cabecera `Content-Disposition`. Como `.encode("ascii",
   "ignore")` no filtra CR ni LF (son ASCII), un nombre malicioso inyectaba
   cabeceras. Derivándolo del contenido, el problema desaparece de raíz.
"""

import re

# (codec, mime, extensión) — el mime es lo que se manda como Content-Type.
UNKNOWN = ("bin", "application/octet-stream", "bin")

# audio/ogg y no audio/opus: Firefox y Chrome son quisquillosos con audio/opus,
# y VLC acepta ambos.
_OGG_CODECS = (
    (b"OpusHead", ("opus", "audio/ogg", "opus")),
    (b"\x01vorbis", ("vorbis", "audio/ogg", "ogg")),
    (b"\x7fFLAC", ("flac", "audio/ogg", "oga")),
)

_FTYP_BRANDS = {
    b"M4A ": ("aac", "audio/mp4", "m4a"),
    b"M4B ": ("aac", "audio/mp4", "m4b"),
    b"mp42": ("aac", "audio/mp4", "m4a"),
    b"isom": ("aac", "audio/mp4", "m4a"),
    b"dash": ("aac", "audio/mp4", "m4a"),
}

EXT_RE = re.compile(r"^[a-z0-9]{1,8}$")


def sniff(head: bytes) -> tuple[str, str, str]:
    """Devuelve (codec, mime, ext) a partir de los primeros bytes del audio."""
    if len(head) < 12:
        return UNKNOWN

    if head[:4] == b"fLaC":
        return ("flac", "audio/flac", "flac")

    if head[:3] == b"ID3":
        return ("mp3", "audio/mpeg", "mp3")

    # MPEG frame sync: 11 bits a 1. Cubre los mp3 sin tag ID3.
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return ("mp3", "audio/mpeg", "mp3")

    if head[:4] == b"OggS":
        # El codec real vive en la primera página, tras la cabecera de 27 B + tabla.
        for magic, result in _OGG_CODECS:
            if magic in head[:64]:
                return result
        return ("ogg", "audio/ogg", "ogg")

    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ("wav", "audio/wav", "wav")

    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12], ("aac", "audio/mp4", "m4a"))

    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ("webm", "audio/webm", "webm")

    if head[:4] == b"caff":
        return ("caf", "audio/x-caf", "caf")

    if head[:4] == b"FORM" and head[8:12] == b"AIFF":
        return ("aiff", "audio/aiff", "aiff")

    if head[:2] == b"\xff\xf1" or head[:2] == b"\xff\xf9":
        return ("aac", "audio/aac", "aac")

    if head[:4] == b"wvpk":
        return ("wavpack", "audio/wavpack", "wv")

    if head[:3] == b"MAC":
        return ("ape", "audio/ape", "ape")

    return UNKNOWN


def mime_for_ext(ext: str) -> str:
    """Fallback cuando solo se conoce la extensión (ficheros v1, que no traen mime)."""
    table = {
        "mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav",
        "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
        "m4a": "audio/mp4", "m4b": "audio/mp4", "aac": "audio/aac",
        "webm": "audio/webm", "aiff": "audio/aiff", "wv": "audio/wavpack",
    }
    return table.get(ext.lower(), "application/octet-stream")


def safe_ext(value: str | None) -> str:
    """Saneado estricto de una extensión que va a acabar en una cabecera HTTP.

    v1 usaba .encode("ascii", "ignore"), que deja pasar \\r y \\n.
    """
    if not value:
        return "bin"
    v = value.strip().lower().lstrip(".")
    return v if EXT_RE.match(v) else "bin"
