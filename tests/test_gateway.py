"""La pasarela: VLC habla con SU nodo y el nodo trae el audio del peer.

El caso de uso completo: el track está SOLO en B; el cliente pide a A. A
resuelve quién lo tiene, traslada el Range del audio al fichero remoto, lo
re-sirve, y en background trae el .tmisc entero verificado — la segunda
reproducción no toca la red.
"""

import os

import pytest
from fastapi.testclient import TestClient

LOOPBACK = ("127.0.0.1", 51000)


@pytest.fixture
def net(p2p, mp3_bytes, flac_bytes):
    """A (vacío) y B (con dos tracks), emparejados. Clientes players de ambos."""
    a = p2p.node("nodo-a")
    b = p2p.node("nodo-b", [(mp3_bytes, "paranoid"), (flac_bytes, "xtal")])
    p2p.pair(a, b)
    return a, b, TestClient(a.app, client=LOOPBACK), TestClient(b.app, client=LOOPBACK)


def _tid(node, title):
    _, tracks = node.catalog.search(q=title)
    return tracks[0].id


def _file_hits(p2p, node):
    return [p for m, p in p2p.log[node.addr] if p.startswith("/p2p/v1/file")]


def test_stream_remoto_completo(net, p2p, mp3_bytes):
    a, b, cli_a, _ = net
    tid = _tid(b, "paranoid")

    r = cli_a.get(f"/stream/{tid}")
    assert r.status_code == 200
    assert r.content == mp3_bytes                      # byte a byte, vía B
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["x-tmisc-source"] == b.identity.node_id[:16]


def test_range_remoto_es_un_slice_correcto(net, p2p, mp3_bytes):
    a, b, cli_a, _ = net
    tid = _tid(b, "paranoid")

    r = cli_a.get(f"/stream/{tid}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    # el Content-Range habla del AUDIO, no del .tmisc del peer
    assert r.headers["content-range"] == f"bytes 100-199/{len(mp3_bytes)}"
    assert r.content == mp3_bytes[100:200]

    r = cli_a.get(f"/stream/{tid}", headers={"Range": "bytes=-100"})
    assert r.content == mp3_bytes[-100:]


def test_head_y_416_no_tocan_el_canal_de_datos(net, p2p, mp3_bytes):
    """La metadata remota basta para HEAD y 416: /p2p/v1/file ni se toca."""
    a, b, cli_a, _ = net
    tid = _tid(b, "paranoid")

    r = cli_a.head(f"/stream/{tid}")
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == len(mp3_bytes)
    assert r.content == b""

    r = cli_a.get(f"/stream/{tid}", headers={"Range": f"bytes={len(mp3_bytes)}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(mp3_bytes)}"

    assert _file_hits(p2p, b) == []


def test_fetch_through_hace_local_la_segunda_reproduccion(net, p2p, mp3_bytes):
    a, b, cli_a, _ = net
    tid = _tid(b, "paranoid")

    assert cli_a.get(f"/stream/{tid}").content == mp3_bytes   # 1ª: proxy + fetch

    # el .tmisc completo quedó en el caché, verificado e indexado
    fetched = os.path.join(a.settings.fetched_dir, f"{tid}.tmisc")
    assert os.path.exists(fetched)
    assert a.catalog.resolve(tid) is not None

    p2p.log[b.addr].clear()
    r = cli_a.get(f"/stream/{tid}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.content == mp3_bytes[:100]
    assert p2p.log[b.addr] == []                       # 2ª: B ni se entera


def test_id_inexistente_404_sin_recursion(net, p2p):
    """A pregunta a B; B (que también tiene a A como peer) responde solo con su
    catálogo local. Sin ciclos, y con caché negativa."""
    a, b, cli_a, _ = net
    nadie = "e" * 64

    assert cli_a.get(f"/stream/{nadie}").status_code == 404
    hits = len(p2p.log[b.addr])
    assert cli_a.get(f"/stream/{nadie}").status_code == 404   # 2ª: caché negativa
    assert len(p2p.log[b.addr]) == hits
    assert p2p.log[a.addr] == []                       # B nunca preguntó a A


def test_track_y_cover_remotos(net, p2p, mp3_bytes):
    a, b, cli_a, _ = net
    tid = _tid(b, "paranoid")

    body = cli_a.get(f"/track/{tid}").json()
    assert body["id"] == tid
    assert body["source"]["node_id"] == b.identity.node_id
    # las URLs apuntan a A (la pasarela), no a B
    assert f"/stream/{tid}" in body["stream_url"]
    assert body["track"]["title"] == "paranoid"

    cover = cli_a.get(f"/cover/{tid}")
    assert cover.status_code == 200
    assert cover.content[:2] == b"BM"                  # el slice [0, image_size) de B


def test_network_catalog_agrega_y_dedupe(net, p2p, mp3_bytes):
    a, b, cli_a, cli_b = net
    tid = _tid(b, "paranoid")

    body = cli_a.get("/network/catalog").json()
    assert body["total"] == 2
    assert {i["source"] for i in body["items"]} == {b.identity.node_id}

    # tras traer un track, ese pasa a ser local y gana el dedupe
    cli_a.post(f"/fetch/{tid}")
    import time
    for _ in range(50):
        if a.catalog.resolve(tid) is not None:
            break
        time.sleep(0.1)
    body = cli_a.get("/network/catalog").json()
    assert body["total"] == 2                          # mismo total: dedupe por id
    sources = {i["id"]: i["source"] for i in body["items"]}
    assert sources[tid] == "local"


def test_fetch_corrupto_no_indexa(net, p2p):
    a, b, cli_a, _ = net
    tid = _tid(b, "xtal")
    row = b.catalog.resolve(tid)

    with open(row["path"], "r+b") as fh:               # B tiene el disco podrido
        fh.seek(row["audio_offset"] + 500)
        byte = fh.read(1)
        fh.seek(row["audio_offset"] + 500)
        fh.write(bytes([byte[0] ^ 0xFF]))

    r = cli_a.get(f"/stream/{tid}")                    # el proxy sirve igual...
    assert r.status_code == 200
    # ...pero el fetch-through detectó el hash roto: ni fichero ni catálogo
    assert os.listdir(a.settings.fetched_dir) == []
    assert a.catalog.resolve(tid) is None


def test_peer_caido_stream_404_y_catalogo_solo_local(net, p2p, mp3_bytes):
    a, b, cli_a, _ = net
    tid = _tid(b, "paranoid")

    # B "se apaga": toda petición hacia él falla en el connect
    import asyncio

    import httpx

    class Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("peer apagado")

    a.network.transport_factory = lambda peer: Down()
    asyncio.run(a.network.drop_client(b.identity.node_id))

    assert cli_a.get(f"/stream/{tid}").status_code == 404
    assert cli_a.get("/network/catalog").json()["total"] == 0
