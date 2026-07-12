"""El gateway de extremo a extremo, con TestClient."""

import pytest
from fastapi.testclient import TestClient

from tmisc.config import PROFILE_NODE, PROFILE_TOOLS, Settings


@pytest.fixture
def client(tmp_path, library):
    lib, _cat = library
    from server.app import create_app

    settings = Settings(
        library=str(lib), cache_dir=str(tmp_path / "cache"),
        profile=PROFILE_NODE,
    ).resolved()
    settings.db_path = str(tmp_path / "catalog.db")   # el que ya pobló el fixture
    return TestClient(create_app(settings))


@pytest.fixture
def track_id(client) -> str:
    items = client.get("/catalog?q=paranoid").json()["items"]
    return items[0]["id"]


def test_catalog(client):
    body = client.get("/catalog").json()
    # 3 ficheros en disco, pero 2 tracks: el fixture v1 lleva el mismo audio que
    # uno de los v2, y el id es el hash del audio -> dedupe.
    assert body["total"] == 2
    assert body["items"][0]["stream_url"].startswith("http")   # URL absoluta, no relativa


def test_sin_range_devuelve_200(client, track_id, mp3_bytes):
    r = client.get(f"/stream/{track_id}")
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/mpeg"
    assert int(r.headers["content-length"]) == len(mp3_bytes)
    assert r.content == mp3_bytes


def test_range_parcial(client, track_id, mp3_bytes):
    r = client.get(f"/stream/{track_id}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    # El total del Content-Range es el tamaño del AUDIO, no el del .tmisc.
    assert r.headers["content-range"] == f"bytes 100-199/{len(mp3_bytes)}"
    assert r.content == mp3_bytes[100:200]


def test_sufijo(client, track_id, mp3_bytes):
    r = client.get(f"/stream/{track_id}", headers={"Range": "bytes=-100"})
    assert r.status_code == 206
    assert r.content == mp3_bytes[-100:]


def test_fuera_de_rango_416_con_content_range(client, track_id, mp3_bytes):
    r = client.get(f"/stream/{track_id}", headers={"Range": f"bytes={len(mp3_bytes)+1}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(mp3_bytes)}"   # obligatorio


def test_head_no_manda_cuerpo(client, track_id, mp3_bytes):
    """Starlette NO suprime el cuerpo solo; VLC y mpv hacen HEAD para sondear seek."""
    r = client.head(f"/stream/{track_id}")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(mp3_bytes)
    assert r.content == b""


def test_reensamblar_desde_dos_ranges(client, track_id, mp3_bytes):
    """La prueba de que el seek devuelve exactamente los bytes correctos."""
    corte = len(mp3_bytes) // 2
    a = client.get(f"/stream/{track_id}", headers={"Range": f"bytes=0-{corte-1}"}).content
    b = client.get(f"/stream/{track_id}", headers={"Range": f"bytes={corte}-"}).content
    assert a + b == mp3_bytes


def test_cors_expuesto(client, track_id):
    """Sin esto, el <audio crossOrigin="anonymous"> de TidolAudioEngine falla opaco."""
    r = client.get(f"/stream/{track_id}", headers={"Origin": "http://localhost:5173"})
    assert r.headers["access-control-allow-origin"] == "*"
    expuestos = r.headers["access-control-expose-headers"].lower()
    assert "content-range" in expuestos and "accept-ranges" in expuestos


def test_etag_inmutable(client, track_id):
    """El id es una content-address: el contenido de la URL no puede cambiar."""
    r = client.get(f"/stream/{track_id}", headers={"Range": "bytes=0-9"})
    assert r.headers["etag"] == f'"{track_id}"'
    assert "immutable" in r.headers["cache-control"]


def test_cover_es_un_slice_de_la_imagen(client, track_id):
    r = client.get(f"/cover/{track_id}")
    assert r.status_code == 200
    assert r.content[:2] == b"BM"


def test_track_json(client, track_id):
    body = client.get(f"/track/{track_id}").json()
    assert body["id"] == track_id
    assert body["track"]["title"] == "Paranoid Android"
    assert body["stream_url"].endswith(".mp3")


def test_playlists(client, track_id):
    m3u = client.get("/playlist.m3u8?q=paranoid")
    assert m3u.headers["content-type"].startswith("audio/x-mpegurl")
    assert "#EXTM3U" in m3u.text

    xspf = client.get("/playlist.xspf?q=paranoid")
    assert xspf.headers["content-type"].startswith("application/xspf+xml")
    assert "<location>http" in xspf.text
    assert "<image>http" in xspf.text          # carátula por pista: VLC la muestra


def test_id_invalido(client):
    assert client.get("/stream/../../etc/passwd").status_code in (400, 404)
    assert client.get("/stream/nohexadecimal").status_code == 400


def test_track_inexistente(client):
    assert client.get("/stream/" + "a" * 64).status_code == 404


def test_perfil_tools_no_expone_streaming(tmp_path):
    """En el perfil `tools` las rutas de audio LITERALMENTE no existen."""
    from server.app import create_app

    settings = Settings(cache_dir=str(tmp_path / "c"), profile=PROFILE_TOOLS).resolved()
    c = TestClient(create_app(settings))

    assert c.get("/health").status_code == 200
    assert c.get("/stream/" + "a" * 64).status_code == 404
    assert c.get("/catalog").status_code == 404


def test_dos_nodos_conviven_en_un_proceso(tmp_path, library):
    """El estado vive en app.state: dos create_app con settings distintos no se
    pisan. Es el prerequisito de los tests de P2P (nodo A y nodo B in-process)."""
    from server.app import create_app

    lib, _cat = library
    s_a = Settings(library=str(lib), cache_dir=str(tmp_path / "ca"),
                   profile=PROFILE_NODE).resolved()
    s_a.db_path = str(tmp_path / "catalog.db")        # el que ya pobló el fixture
    s_b = Settings(library=str(tmp_path / "vacia"), cache_dir=str(tmp_path / "cb"),
                   profile=PROFILE_NODE).resolved()

    a = TestClient(create_app(s_a))
    b = TestClient(create_app(s_b))

    assert a.get("/catalog").json()["total"] == 2     # A sigue viendo SU catálogo
    assert b.get("/catalog").json()["total"] == 0     # B ve el suyo, vacío
