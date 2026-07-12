"""El patrón "Range -> 200/206/416 + HEAD sin cuerpo", una sola vez.

Lo usan /stream y /file del app principal, /p2p/v1/file del listener P2P y la
pasarela que re-sirve audio de un peer. Las trampas (HEAD explícito, el 416 con
Content-Range obligatorio, Content-Length exacto) viven aquí y se testean una
sola vez.
"""

from typing import Callable, Iterable

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from tmisc.container import CHUNK
from tmisc.ranges import RangeUnsatisfiable, parse_range, unsatisfiable_headers


def file_slice(path: str, start: int, length: int, chunk: int = CHUNK) -> Iterable[bytes]:
    """Bytes crudos [start, start+length) de un fichero, por trozos."""
    with open(path, "rb", buffering=0) as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            data = fh.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def range_response(
    request: Request,
    total: int,
    headers: dict[str, str],
    opener: Callable[[int, int], Iterable[bytes]],
) -> Response:
    """Responde una petición GET/HEAD con soporte de Range sobre `total` bytes.

    `opener(start, length)` produce los bytes; solo se invoca en un GET con
    rango satisfacible. `headers` debe traer ya Content-Type y demás cabeceras
    del recurso; aquí se añaden Content-Length y Content-Range.
    """
    try:
        rng = parse_range(request.headers.get("range"), total)
    except RangeUnsatisfiable:
        return Response(status_code=416, headers={**headers, **unsatisfiable_headers(total)})

    if rng is None:
        status, start, length = 200, 0, total
    else:
        status, start, length = 206, rng.start, rng.length
        headers["Content-Range"] = rng.content_range(total)
    headers["Content-Length"] = str(length)

    if request.method == "HEAD":
        return Response(status_code=status, headers=headers)

    return StreamingResponse(opener(start, length), status_code=status, headers=headers)
