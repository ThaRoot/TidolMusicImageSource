"""Los tres bugs que tenía v1, con su regresión."""

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tmisc.config import PROFILE_TOOLS, Settings
from tmisc.errors import TmiscError
from tmisc.sniff import safe_ext


@pytest.fixture
def tools(tmp_path):
    from server.app import create_app

    settings = Settings(cache_dir=str(tmp_path / "c"), profile=PROFILE_TOOLS,
                        max_upload=4 << 20).resolved()
    return TestClient(create_app(settings))


# --- 1. Inyección de cabeceras vía el campo `fmt` -----------------------------
# v1 sanitizaba con .encode("ascii", "ignore"), pero \r y \n SON ASCII y
# sobrevivían al filtro. El `fmt` venía del nombre del fichero subido y acababa
# dentro de `Content-Disposition: filename="recovered.{fmt}"`.
@pytest.mark.parametrize("malicioso", [
    "a\r\nX-Evil: 1",
    "mp3\r\nSet-Cookie: x=y",
    "../../etc/passwd",
    'x"; drop',
    "\n\n<script>",
])
def test_no_se_pueden_inyectar_cabeceras_por_la_extension(malicioso):
    limpio = safe_ext(malicioso)
    assert "\r" not in limpio and "\n" not in limpio
    assert limpio.isalnum() or limpio == "bin"


def test_decode_deriva_la_extension_del_contenido(tools, tmp_path):
    """Aunque el fichero se llame de forma maliciosa, la extensión sale de los magic bytes."""
    import os

    from tmisc.encode import encode_to_file

    with open(os.path.join(os.path.dirname(__file__), "data", "tone.mp3"), "rb") as fh:
        mp3 = fh.read()

    out = str(tmp_path / "x.tmisc")
    encode_to_file(io.BytesIO(mp3), len(mp3), out, source_filename="a\r\nX-Evil: 1.mp3")

    with open(out, "rb") as fh:
        r = tools.post("/decode", files={"file": ("evil\r\n.tmisc", fh, "image/bmp")})

    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="recovered.mp3"'
    for value in r.headers.values():
        assert "\r" not in value and "\n" not in value


# --- 2. /decode sin límite de tamaño -----------------------------------------
# v1: `await file.read()` materializaba el upload entero en RAM y MAX_BYTES solo
# se comprobaba en /encode (y encima después de haber leído todo).
def test_decode_rechaza_uploads_gigantes(tools):
    gordo = b"\x00" * (5 << 20)          # 5 MB contra un límite de 4 MB
    r = tools.post("/decode", files={"file": ("big.tmisc", io.BytesIO(gordo), "image/bmp")})
    assert r.status_code == 413


def test_encode_rechaza_audio_gigante(tools):
    gordo = b"\x00" * (5 << 20)
    r = tools.post("/encode", files={"audio": ("big.wav", io.BytesIO(gordo), "audio/wav")})
    assert r.status_code == 413


# --- 3. Bomba de descompresión de Pillow --------------------------------------
# v1: Image.open() sobre bytes arbitrarios. Un PNG de 1 KB puede declarar
# 40000x40000 y reventar la RAM al hacer .convert("RGB"). Pillow, por defecto,
# solo EMITE un warning.
def test_cover_bomba_se_rechaza():
    from tmisc.cover import load_cover

    # PNG minúsculo que declara una imagen enorme
    bomba = io.BytesIO()
    Image.new("L", (12000, 12000)).save(bomba, format="PNG", compress_level=9)

    with pytest.raises(TmiscError):
        load_cover(bomba.getvalue())


def test_cover_no_es_imagen():
    from tmisc.cover import load_cover

    with pytest.raises(TmiscError):
        load_cover(b"esto no es una imagen")


def test_formato_de_imagen_no_permitido():
    from tmisc.cover import load_cover

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    with pytest.raises(TmiscError):
        load_cover(svg)


# --- 4. El trailer v1 podía apuntar dentro del BMP ----------------------------
def test_trailer_v1_manipulado_no_puede_solapar_la_imagen(tmp_path):
    """v1 solo comprobaba `start < 0`: nada impedía que el audio solapara el BMP."""
    import struct

    from tmisc import format as fmt
    from tmisc.container import probe
    from tmisc.errors import TmiscError as TE

    # BMP mínimo + trailer que declara un audio más grande que el propio fichero
    bmp = io.BytesIO()
    Image.new("RGB", (16, 16)).save(bmp, format="BMP")
    data = bmp.getvalue()

    trailer = struct.pack(fmt.TRAILER_V1_FMT, fmt.MAGIC_V1, fmt.MODE_APPEND_V1,
                          b"mp3\x00\x00\x00\x00\x00", 10 ** 6, 0)
    path = str(tmp_path / "malo.tmisc")
    with open(path, "wb") as fh:
        fh.write(data + trailer)

    with pytest.raises(TE):
        probe(path)
