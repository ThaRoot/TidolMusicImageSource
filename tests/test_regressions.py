"""Los tres fallos que se comieron una sesión de pruebas real por Tailscale.

1. La playlist no propagaba el token: VLC bajaba la lista y se comía un 401 en
   CADA pista. La playlist "funcionaba" y no sonaba nada.
2. IDENT_RE aceptaba prefijos de 8..64 hex, pero el catálogo hacía exact match,
   así que todo id corto daba 404. Teclear 64 hex a mano en un móvil, con un
   carácter de menos, fue exactamente cómo salió el bug.
3. El escaneo de arranque solo miraba library/, y como olvida todo path conocido
   que no vuelve a ver, desindexaba en cada reinicio lo que la pasarela había
   traído del peer: el .tmisc seguía en disco y el nodo se lo volvía a pedir.
"""

import os

from fastapi.testclient import TestClient

from tmisc.catalog import Catalog
from tmisc.config import PROFILE_NODE, Settings
from tmisc.scanner import scan

LOOPBACK = ("127.0.0.1", 51000)


def _client(tmp_path, library, token: str = "") -> TestClient:
    from server.app import create_app

    lib, _cat = library
    settings = Settings(
        library=str(lib), cache_dir=str(tmp_path / "cache"),
        profile=PROFILE_NODE, token=token,
    ).resolved()
    settings.db_path = str(tmp_path / "catalog.db")   # el que ya pobló el fixture
    return TestClient(create_app(settings))


# --- 1. el token tiene que viajar dentro de la playlist -------------------------

def test_playlist_lleva_el_token_en_cada_pista(tmp_path, library):
    cli = _client(tmp_path, library, token="s3cr3t")

    locations = [ln for ln in cli.get("/playlist.xspf?k=s3cr3t").text.splitlines()
                 if "<location>" in ln]
    assert locations
    assert all("?k=s3cr3t" in ln for ln in locations)

    urls = [ln for ln in cli.get("/playlist.m3u8?k=s3cr3t").text.splitlines()
            if ln.startswith("http")]
    assert urls
    assert all("?k=s3cr3t" in u for u in urls)


def test_las_pistas_de_la_playlist_reproducen(tmp_path, library):
    """El fallo de verdad: la lista se bajaba, pero cada <location> daba 401."""
    cli = _client(tmp_path, library, token="s3cr3t")

    body = cli.get("/playlist.xspf?k=s3cr3t").text
    location = body.split("<location>")[1].split("</location>")[0]
    path = location.split("testserver", 1)[1]     # base_url sale del header Host

    assert cli.get(path).status_code == 200


def test_sin_token_las_urls_quedan_limpias(tmp_path, library):
    assert "?k=" not in _client(tmp_path, library).get("/playlist.xspf").text


# --- 2. un id corto identifica un track ----------------------------------------

def test_prefijo_corto_reproduce(tmp_path, library):
    cli = _client(tmp_path, library)
    full = cli.get("/catalog?q=paranoid").json()["items"][0]["id"]

    r = cli.get(f"/stream/{full[:8]}")
    assert r.status_code == 200
    assert r.content == cli.get(f"/stream/{full}").content

    # El id canónico manda: de él cuelgan el ETag y la verificación del BLAKE3.
    assert r.headers["x-tmisc-id"] == full
    assert r.headers["etag"] == cli.get(f"/stream/{full}").headers["etag"]
    assert r.headers["etag"].startswith(f'"{full[:32]}-')


def test_prefijo_corto_en_track_cover_y_file(tmp_path, library):
    cli = _client(tmp_path, library)
    full = cli.get("/catalog?q=paranoid").json()["items"][0]["id"]

    assert cli.get(f"/track/{full[:10]}").json()["id"] == full
    assert cli.get(f"/cover/{full[:10]}").status_code == 200
    assert cli.get(f"/file/{full[:10]}").status_code == 200


def test_prefijo_de_menos_de_8_es_400(tmp_path, library):
    assert _client(tmp_path, library).get("/stream/abc").status_code == 400


def test_prefijo_ambiguo_no_resuelve(tmp_path):
    """Elegir "el primero" entre dos tracks que comparten prefijo sería servir en
    silencio la canción equivocada: peor que un 404."""
    cat = Catalog(str(tmp_path / "c.db"))
    uno, dos = "abcdef01" + "0" * 56, "abcdef01" + "1" * 56
    for tid in (uno, dos):
        cat.db.execute(
            """INSERT INTO tracks (id, audio_size, mime, ext, indexed_at)
               VALUES (?, 1, 'audio/mpeg', 'mp3', 0)""",
            (tid,),
        )
    cat.db.commit()

    assert cat.expand_id(uno) == uno                  # completo
    assert cat.expand_id("abcdef010") == uno          # inequívoco en el 9º carácter
    assert cat.expand_id("abcdef01") is None          # ambiguo: NO resuelve
    assert cat.expand_id("ffffffff") is None          # no existe
    assert cat.resolve("abcdef01") is None


# --- 3. lo traído del peer sobrevive al reinicio --------------------------------

def test_el_rescan_no_desindexa_lo_traido_del_peer(p2p, mp3_bytes):
    a = p2p.node("nodo-a")
    b = p2p.node("nodo-b", [(mp3_bytes, "paranoid")])
    p2p.pair(a, b)

    _, tracks = b.catalog.search(q="paranoid")
    tid = tracks[0].id

    # La pasarela sirve el track de B y, de paso, se lo trae al caché.
    assert TestClient(a.app, client=LOOPBACK).get(f"/stream/{tid}").status_code == 200
    assert os.path.exists(os.path.join(a.settings.fetched_dir, f"{tid}.tmisc"))
    assert a.catalog.resolve(tid) is not None

    # El escaneo que hace `serve` al arrancar. Antes lo olvidaba y A volvía a
    # pedirle a B un fichero que ya tenía en disco.
    scan(a.settings.scan_roots, a.catalog)
    assert a.catalog.resolve(tid) is not None


def test_scan_roots_incluye_el_directorio_fetched(p2p):
    a = p2p.node("nodo-a")
    assert a.settings.fetched_dir in a.settings.scan_roots


def test_scan_acepta_varias_raices_sin_pisarse(tmp_path, library):
    """Dos raíces en UNA llamada, no dos llamadas: la segunda pasada olvidaría
    todo lo que encontró la primera."""
    lib, _ = library
    otra = tmp_path / "otra"
    otra.mkdir()

    cat = Catalog(str(tmp_path / "multi.db"))
    scan([str(lib), str(otra)], cat)
    assert cat.count() == 2                      # las mismas que ve el fixture

    scan(str(lib), cat)                          # una sola raíz: nada que olvidar
    assert cat.count() == 2
