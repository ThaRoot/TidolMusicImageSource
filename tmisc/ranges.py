"""Cabecera `Range` (RFC 9110 §14) — lo que hace posible el seek.

Los casos no son teóricos, cada uno lo usa un cliente real:

  ausente          primera petición de VLC y mpv
  bytes=0-         Chrome y el <audio> del navegador SIEMPRE abren así
  bytes=S-E        seek en VLC
  bytes=-N         SUFIJO: Firefox lo usa para cazar el átomo `moov` de un m4a
                   cuando está al final; VLC lo usa en varios demuxers
  fuera de rango   -> 416, y el Content-Range es OBLIGATORIO en la respuesta
  multi-range      no lo usa prácticamente nadie; la RFC permite ignorarlo y
                   responder 200 con el cuerpo entero, así que eso hacemos en
                   lugar de implementar multipart/byteranges
"""

from dataclasses import dataclass


class RangeUnsatisfiable(Exception):
    """El rango pedido no solapa el recurso: toca 416."""


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int            # inclusivo, como en HTTP

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self, total: int) -> str:
        return f"bytes {self.start}-{self.end}/{total}"


def parse_range(value: str | None, total: int) -> ByteRange | None:
    """Devuelve el rango pedido, o None si hay que responder 200 con todo.

    Lanza RangeUnsatisfiable si el rango es válido sintácticamente pero cae
    fuera del recurso.
    """
    if not value:
        return None

    value = value.strip()
    if not value.lower().startswith("bytes="):
        return None                      # otra unidad: se ignora -> 200

    spec = value[6:].strip()
    if "," in spec:
        return None                      # multi-range: 200 con el cuerpo entero

    if "-" not in spec:
        return None                      # sintaxis inválida: se ignora -> 200

    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    if total <= 0:
        raise RangeUnsatisfiable()

    # --- sufijo: "bytes=-N" = los últimos N bytes ---
    if not first:
        if not last.isdigit():
            return None
        n = int(last)
        if n == 0:
            raise RangeUnsatisfiable()
        start = max(0, total - n)
        return ByteRange(start, total - 1)

    if not first.isdigit():
        return None
    start = int(first)

    if start >= total:
        raise RangeUnsatisfiable()

    if not last:
        end = total - 1                  # "bytes=S-"
    else:
        if not last.isdigit():
            return None
        end = min(int(last), total - 1)  # clamp: pedir de más no es un error

    if start > end:
        raise RangeUnsatisfiable()

    return ByteRange(start, end)


def unsatisfiable_headers(total: int) -> dict[str, str]:
    """El 416 DEBE llevar Content-Range con el tamaño real, o el cliente se pierde."""
    return {"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"}
