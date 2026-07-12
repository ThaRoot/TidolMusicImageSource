"""La tabla de casos de Range. Cada fila es un cliente real."""

import pytest

from tmisc.ranges import RangeUnsatisfiable, parse_range

TOTAL = 1000


@pytest.mark.parametrize(
    "header,expected,quien",
    [
        (None,            None,        "primera petición de VLC/mpv -> 200"),
        ("",              None,        "cabecera vacía -> 200"),
        ("bytes=0-",      (0, 999),    "Chrome y el <audio> SIEMPRE abren así"),
        ("bytes=0-99",    (0, 99),     "sonda inicial"),
        ("bytes=500-599", (500, 599),  "seek en VLC"),
        ("bytes=500-",    (500, 999),  "seek hasta el final"),
        ("bytes=-100",    (900, 999),  "SUFIJO: Firefox, para el moov de un m4a"),
        ("bytes=-5000",   (0, 999),    "sufijo mayor que el recurso -> todo"),
        ("bytes=0-99999", (0, 999),    "fin clampado: pedir de más no es un error"),
        ("bytes=999-999", (999, 999),  "el último byte"),
        ("bytes=0-9,20-29", None,      "multi-range -> 200 con el cuerpo entero (RFC 9110)"),
        ("items=0-10",    None,        "otra unidad -> se ignora"),
        ("bytes=abc-def", None,        "sintaxis inválida -> se ignora"),
    ],
)
def test_parse_range(header, expected, quien):
    got = parse_range(header, TOTAL)
    if expected is None:
        assert got is None, quien
    else:
        assert (got.start, got.end) == expected, quien


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000", "bytes=-0", "bytes=900-100"])
def test_unsatisfiable(header):
    """Estos DEBEN dar 416, y el 416 lleva Content-Range obligatorio."""
    with pytest.raises(RangeUnsatisfiable):
        parse_range(header, TOTAL)


def test_length_y_content_range():
    r = parse_range("bytes=10-19", TOTAL)
    assert r.length == 10
    # El total del Content-Range es el tamaño del AUDIO, no el del .tmisc.
    assert r.content_range(TOTAL) == "bytes 10-19/1000"


def test_recurso_vacio():
    with pytest.raises(RangeUnsatisfiable):
        parse_range("bytes=0-", 0)
