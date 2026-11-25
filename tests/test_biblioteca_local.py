"""Etapa 1: la biblioteca se sirve en su sitio y el id ignora las etiquetas.

Ver docs/VISION.md. Lo que se fija aquí:
  - editar tags o la portada NO cambia el id (mp3, flac, opus, ogg, m4a, wav);
  - la misma canción suelta y dentro de un .tmisc es UN track;
  - un mp3/flac se indexa sin convertirlo, se sirve entero y con su portada;
  - el ETag sí cambia al editar tags (los bytes servidos cambian);
  - por P2P, un audio suelto llega con su extensión, verificado por id Y por
    hash del fichero entero (un byte corrupto en el ID3 no pasa).
"""

import base64
import io
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from tmisc.audioid import content_id, content_id_bytes
from tmisc.encode import encode_to_file
from tmisc.meta import TrackMeta

DATA = os.path.join(os.path.dirname(__file__), "data")
HAS_FFMPEG = shutil.which("ffmpeg") is not None
LOOPBACK = ("127.0.0.1", 51000)


def cid(path):
    with open(path, "rb") as fh:
        return content_id(fh)


def jpeg(color=(200, 30, 30), extra=0) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="JPEG")
    return buf.getvalue() + os.urandom(extra)


def copy(tmp_path, name, dest=None):
    dst = tmp_path / (dest or name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.join(DATA, name), dst)
    return str(dst)


def ffmpeg(src, dst, *args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", src, "-t", "2", *args, dst],
                   check=True)
    return dst


def retag_easy(path, **tags):
    import mutagen
    mf = mutagen.File(path, easy=True)
    if mf.tags is None:
        mf.add_tags()
    for k, v in tags.items():
        mf[k] = v
    mf.save()


# --- el id ignora las etiquetas ---------------------------------------------

def test_mp3_id3v2_apic_apev2_e_id3v1_no_cambian_el_id(tmp_path):
    from mutagen.apev2 import APEv2
    from mutagen.id3 import APIC, ID3

    p = copy(tmp_path, "tone.mp3")
    before = cid(p)
    retag_easy(p, title="otro título", artist="otro artista")
    tags = ID3(p)
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, data=jpeg(extra=50_000)))
    tags.save(p, v1=2)                               # v1=2: escribe también ID3v1
    ape = APEv2()
    ape["Title"] = "ape"
    ape.save(p)
    with open(p, "rb") as fh:
        data = fh.read()
    assert data[-32:-24] == b"APETAGEX" or b"APETAGEX" in data[-300:]

    assert cid(p) == before
    assert before[1] is True


def test_flac_comentarios_y_picture_no_cambian_el_id(tmp_path):
    from mutagen.flac import FLAC, Picture

    p = copy(tmp_path, "tone.flac")
    before = cid(p)
    f = FLAC(p)
    f["title"] = "cambiado"
    pic = Picture()
    pic.data, pic.type, pic.mime = jpeg(extra=80_000), 3, "image/jpeg"
    f.add_picture(pic)
    f.save()
    assert cid(p) == before and before[1]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg no disponible")
@pytest.mark.parametrize("ext,args", [("opus", []), ("ogg", ["-c:a", "libvorbis"]),
                                      ("m4a", ["-c:a", "aac"])])
def test_ogg_opus_m4a_tags_y_portada_no_cambian_el_id(tmp_path, ext, args):
    import mutagen
    from mutagen.flac import Picture

    p = ffmpeg(os.path.join(DATA, "tone.flac"), str(tmp_path / f"t.{ext}"), *args)
    before = cid(p)
    retag_easy(p, title="cambiado")
    mf = mutagen.File(p)
    big = jpeg(extra=150_000)                        # varias páginas Ogg
    if ext == "m4a":
        from mutagen.mp4 import MP4Cover
        mf["covr"] = [MP4Cover(big, imageformat=MP4Cover.FORMAT_JPEG)]
    else:
        pic = Picture()
        pic.data, pic.type, pic.mime = big, 3, "image/jpeg"
        mf["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
    mf.save()
    assert cid(p) == before and before[1]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg no disponible")
def test_wav_chunk_id3_no_cambia_el_id(tmp_path):
    from mutagen.id3 import TIT2
    from mutagen.wave import WAVE

    p = ffmpeg(os.path.join(DATA, "tone.flac"), str(tmp_path / "t.wav"))
    before = cid(p)
    w = WAVE(p)
    w.add_tags()
    w.tags.add(TIT2(encoding=3, text="cambiado"))
    w.save()
    assert cid(p) == before and before[1]


def test_audio_distinto_da_id_distinto(tmp_path):
    a = copy(tmp_path, "tone.mp3")
    with open(a, "r+b") as fh:                       # un byte de audio, al final
        fh.seek(-500, 2)
        b = fh.read(1)
        fh.seek(-500, 2)
        fh.write(bytes([b[0] ^ 0xFF]))
    with open(os.path.join(DATA, "tone.mp3"), "rb") as fh:
        assert content_id(fh) != cid(a)


def test_formato_desconocido_es_id_debil():
    _id, strong = content_id_bytes(b"esto no es audio" * 100)
    assert strong is False


# --- biblioteca en su sitio ---------------------------------------------------

@pytest.fixture
def plain_lib(tmp_path, cover_bytes):
    """mp3 con portada embebida y MBID, flac sin portada con cover.jpg en la carpeta,
    y la MISMA canción del mp3 empaquetada como .tmisc."""
    from mutagen.id3 import APIC, ID3

    lib = tmp_path / "lib"
    mp3 = copy(tmp_path, "tone.mp3", "lib/Radiohead/OK Computer/01 Paranoid.mp3")
    retag_easy(mp3, title="Paranoid Android", artist="Radiohead", album="OK Computer",
               musicbrainz_trackid="8cc4a1b4-0d3a-4e6d-9d84-0123456789ab")
    t = ID3(mp3)
    t.add(APIC(encoding=3, mime="image/png", type=3, data=cover_bytes))
    t.save(mp3)

    flac = copy(tmp_path, "tone.flac", "lib/Aphex Twin/Xtal.flac")
    with open(os.path.join(os.path.dirname(flac), "cover.jpg"), "wb") as fh:
        fh.write(jpeg((10, 200, 10)))

    with open(mp3, "rb") as fh:
        data = fh.read()
    encode_to_file(io.BytesIO(data), len(data), str(lib / "empaquetado.tmisc"),
                   cover_data=cover_bytes, track=TrackMeta(title="Paranoid (tmisc)"))
    return lib, mp3, flac


@pytest.fixture
def node(tmp_path, plain_lib):
    from server.app import create_app
    from tmisc.catalog import Catalog
    from tmisc.config import PROFILE_NODE, Settings
    from tmisc.scanner import scan

    lib, mp3, flac = plain_lib
    settings = Settings(library=str(lib), cache_dir=str(tmp_path / "cache"),
                        profile=PROFILE_NODE).resolved()
    settings.ensure_dirs()
    cat = Catalog(settings.db_path)
    stats = scan(settings.scan_roots, cat)
    assert stats.failed == 0, stats.errors
    return cat, TestClient(create_app(settings, catalog=cat)), mp3, flac, settings


def test_indexa_sin_convertir_y_deduplica_con_tmisc(node):
    cat, _cli, mp3, flac, _s = node
    assert cat.count() == 2                       # mp3 y su .tmisc son UN track
    mid = cid(mp3)[0]
    files = cat.db.execute("SELECT path FROM files WHERE id=?", (mid,)).fetchall()
    assert len(files) == 2
    row = cat.db.execute("SELECT * FROM tracks WHERE id=?", (cid(flac)[0],)).fetchone()
    assert row["ext"] == "flac"


def test_lee_tags_y_mbid(node):
    cat, cli, mp3, _flac, _s = node
    mid = cid(mp3)[0]
    row = cat.db.execute("SELECT title, artist, recording_mbid FROM tracks WHERE id=?",
                         (mid,)).fetchone()
    assert row["artist"] == "Radiohead"
    assert row["recording_mbid"] == "8cc4a1b4-0d3a-4e6d-9d84-0123456789ab"


def test_stream_sirve_el_fichero_tal_cual_con_range(node):
    cat, cli, _mp3, flac, _s = node
    fid = cid(flac)[0]
    with open(flac, "rb") as fh:
        data = fh.read()
    r = cli.get(f"/stream/{fid}")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"].startswith("audio/flac")
    r = cli.get(f"/stream/{fid}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]


def test_portada_embebida_y_luego_la_de_carpeta(node, cover_bytes):
    from mutagen.flac import FLAC, Picture

    _cat, cli, _mp3, flac, _s = node
    fid = cid(flac)[0]
    r = cli.get(f"/cover/{fid}.jpg?size=64")         # sin embebida: cover.jpg (verde)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert "immutable" not in r.headers["cache-control"]

    f = FLAC(flac)                                    # ahora le embebemos un PNG
    pic = Picture()
    pic.data, pic.type, pic.mime = cover_bytes, 3, "image/png"
    f.add_picture(pic)
    f.save()
    r = cli.get(f"/cover/{fid}")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"      # gana la embebida


def test_sin_portada_404(tmp_path):
    from server.app import create_app
    from tmisc.catalog import Catalog
    from tmisc.config import PROFILE_NODE, Settings
    from tmisc.scanner import scan

    copy(tmp_path, "tone.flac", "lib/x.flac")
    s = Settings(library=str(tmp_path / "lib"), cache_dir=str(tmp_path / "c"),
                 profile=PROFILE_NODE).resolved()
    s.ensure_dirs()
    cat = Catalog(s.db_path)
    scan(s.scan_roots, cat)
    cli = TestClient(create_app(s, catalog=cat))
    assert cli.get(f"/cover/{cid(str(tmp_path / 'lib/x.flac'))[0]}").status_code == 404


def test_file_de_audio_suelto_lleva_su_extension(node):
    _cat, cli, _mp3, flac, _s = node
    r = cli.get(f"/file/{cid(flac)[0]}")
    assert r.headers["content-type"].startswith("audio/flac")
    assert r.headers["content-disposition"].endswith('.flac"')


def test_retag_mantiene_id_cambia_etag_y_actualiza_titulo(node):
    from tmisc.scanner import scan

    cat, cli, _mp3, flac, settings = node
    fid = cid(flac)[0]
    etag = cli.get(f"/stream/{fid}").headers["etag"]
    retag_easy(flac, title="Xtal (remaster)")
    scan(settings.scan_roots, cat)
    assert cid(flac)[0] == fid
    assert cli.get(f"/stream/{fid}").headers["etag"] != etag
    row = cat.db.execute("SELECT title FROM tracks WHERE id=?", (fid,)).fetchone()
    assert row["title"] == "Xtal (remaster)"


def test_descriptor_anuncia_el_esquema_de_id(node):
    _cat, cli, *_ = node
    assert cli.get("/.well-known/tmisc.json").json()["id_scheme"] == "b3-untagged-1"


# --- P2P con audio suelto -----------------------------------------------------

@pytest.fixture
def net_plain(p2p, tmp_path):
    """A vacío; B con un flac suelto (sin .tmisc). Emparejados."""
    from tmisc.scanner import scan

    a = p2p.node("nodo-a")
    b = p2p.node("nodo-b")
    shutil.copy(os.path.join(DATA, "tone.flac"), os.path.join(b.settings.library, "xtal.flac"))
    retag_easy(os.path.join(b.settings.library, "xtal.flac"), title="xtal")
    scan(b.settings.scan_roots, b.catalog)
    p2p.pair(a, b)
    return a, b, TestClient(a.app, client=LOOPBACK)


def test_p2p_audio_suelto_llega_con_su_extension_y_se_indexa(net_plain):
    a, b, cli_a = net_plain
    fid = b.catalog.search(q="xtal")[1][0].id
    path_b = b.catalog.resolve(fid)["path"]
    with open(path_b, "rb") as fh:
        data = fh.read()

    assert cli_a.get(f"/stream/{fid}").content == data
    fetched = os.path.join(a.settings.fetched_dir, f"{fid}.flac")
    assert os.path.exists(fetched)
    assert a.catalog.resolve(fid) is not None


def test_p2p_byte_corrupto_en_las_etiquetas_no_se_acepta(net_plain):
    """El id deja fuera los tags; el hash del fichero entero los cubre."""
    a, b, cli_a = net_plain
    fid = b.catalog.search(q="xtal")[1][0].id
    path_b = b.catalog.resolve(fid)["path"]
    with open(path_b, "r+b") as fh:                 # dentro del bloque VORBIS_COMMENT
        data = fh.read()
        pos = data.index(b"xtal")
        fh.seek(pos)
        fh.write(b"XTAL")
    assert cid(path_b)[0] == fid                     # el id NO lo detectaría…

    cli_a.get(f"/stream/{fid}")
    assert os.listdir(a.settings.fetched_dir) == []  # …el hash del fichero sí
    assert a.catalog.resolve(fid) is None
