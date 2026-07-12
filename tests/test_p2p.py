"""El canal entre nodos: handshake, sesiones, /p2p/v1/* y descarga verificada.

Dos nodos completos (A y B) conviven en el proceso — por eso el estado vive en
app.state. El PeerClient de A recibe como transporte un ASGITransport montado
sobre la peer app de B: el protocolo entero (challenge, firma, Bearer, Range,
verificación BLAKE3) se ejercita in-process. El TLS pineado de producción se
valida aparte con un handshake real en test_identity.py.
"""

import asyncio
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def nodes(p2p, mp3_bytes, flac_bytes):
    """A sin biblioteca; B con dos tracks. Emparejados."""
    a = p2p.node("nodo-a")
    b = p2p.node("nodo-b", [(mp3_bytes, "paranoid"), (flac_bytes, "xtal")])
    p2p.pair(a, b)
    return a, b


def run(coro):
    return asyncio.run(coro)


# --- handshake ----------------------------------------------------------------

def test_handshake_y_catalogo(nodes, p2p):
    a, b = nodes

    async def main():
        c = p2p.client(a, b)
        items = await c.catalog()
        await c.aclose()
        return items

    items = run(main())
    assert {i["title"] for i in items} == {"paranoid", "xtal"}


def test_peer_no_emparejado_403(nodes, p2p):
    a, b = nodes
    intruso = p2p.node("intruso")
    intruso.peers.add(a.peers.get(b.identity.node_id))   # conoce el cert de B...

    async def main():
        from tmisc.peerclient import PeerNotPaired
        c = p2p.client(intruso, b)
        with pytest.raises(PeerNotPaired):
            await c.catalog()
        await c.aclose()

    run(main())


def test_firma_invalida_401(nodes):
    a, b = nodes
    tc = TestClient(b.peer_app)

    challenge = tc.post("/p2p/v1/session/challenge",
                        json={"node_id": a.identity.node_id}).json()["challenge"]
    r = tc.post("/p2p/v1/session", json={
        "node_id": a.identity.node_id,
        "challenge": challenge,
        "sig": "00" * 64,                       # firma basura
    })
    assert r.status_code == 401


def test_challenge_es_de_un_solo_uso(nodes):
    a, b = nodes
    from tmisc.peers import session_message

    tc = TestClient(b.peer_app)
    challenge = tc.post("/p2p/v1/session/challenge",
                        json={"node_id": a.identity.node_id}).json()["challenge"]
    sig = a.identity.sign(session_message(
        challenge, a.identity.node_id, b.identity.node_id, b.identity.cert_pem)).hex()
    body = {"node_id": a.identity.node_id, "challenge": challenge, "sig": sig}

    assert tc.post("/p2p/v1/session", json=body).status_code == 200
    assert tc.post("/p2p/v1/session", json=body).status_code == 401   # replay


def test_firma_no_sirve_para_otro_nodo(nodes, p2p, mp3_bytes):
    """Channel binding: la firma incluye la identidad Y el cert del destino."""
    a, b = nodes
    c = p2p.node("nodo-c", [(mp3_bytes, "otra")])
    p2p.pair(a, c)
    from tmisc.peers import session_message

    tc_c = TestClient(c.peer_app)
    challenge = tc_c.post("/p2p/v1/session/challenge",
                          json={"node_id": a.identity.node_id}).json()["challenge"]
    # A firma como si hablase con B; un MITM reenvía esa firma hacia C.
    sig_para_b = a.identity.sign(session_message(
        challenge, a.identity.node_id, b.identity.node_id, b.identity.cert_pem)).hex()
    r = tc_c.post("/p2p/v1/session", json={
        "node_id": a.identity.node_id, "challenge": challenge, "sig": sig_para_b,
    })
    assert r.status_code == 401


def test_datos_sin_sesion_401(nodes):
    _a, b = nodes
    tc = TestClient(b.peer_app)
    algun_id = "a" * 64
    assert tc.get(f"/p2p/v1/file/{algun_id}").status_code == 401
    assert tc.get(f"/p2p/v1/track/{algun_id}").status_code == 401
    assert tc.get("/p2p/v1/catalog").status_code == 401


def test_sesion_caducada_rehandshake_transparente(nodes, p2p):
    a, b = nodes

    async def main():
        c = p2p.client(a, b)
        await c.catalog()
        b.sessions.invalidate_peer(a.identity.node_id)   # caducidad simulada
        return await c.catalog()                          # 401 -> re-handshake -> ok

    assert len(run(main())) == 2


def test_revocacion_corta_las_sesiones_vivas(nodes, p2p):
    a, b = nodes

    async def main():
        c = p2p.client(a, b)
        await c.catalog()                                # sesión viva
        b.peers.remove(a.identity.node_id)
        b.sessions.invalidate_peer(a.identity.node_id)   # lo que hace DELETE /peers
        from tmisc.peerclient import PeerNotPaired
        with pytest.raises(PeerNotPaired):
            await c.catalog()
        await c.aclose()

    run(main())


# --- datos ----------------------------------------------------------------------

def _track_id(node, title: str) -> str:
    _, tracks = node.catalog.search(q=title)
    return tracks[0].id


def test_range_remoto_byte_identico(nodes, p2p, mp3_bytes):
    a, b = nodes
    tid = _track_id(b, "paranoid")
    row = b.catalog.resolve(tid)

    async def main():
        c = p2p.client(a, b)
        # el slice del AUDIO trasladado al fichero: [audio_offset+100, +199]
        off = row["audio_offset"]
        resp = await c.open_file_range(tid, off + 100, off + 199)
        data = b"".join([chunk async for chunk in resp.aiter_bytes()])
        await resp.aclose()
        await c.aclose()
        return resp.status_code, data

    status, data = run(main())
    assert status == 206
    assert data == mp3_bytes[100:200]


def test_track_remoto_trae_offsets(nodes, p2p):
    a, b = nodes
    tid = _track_id(b, "xtal")

    async def main():
        c = p2p.client(a, b)
        meta = await c.track(tid)
        missing = await c.track("f" * 64)
        await c.aclose()
        return meta, missing

    meta, missing = run(main())
    assert missing is None
    assert meta["id"] == tid
    assert meta["audio_offset"] > 0 and meta["audio_size"] > 0
    assert meta["mime"].startswith("audio/")


def test_descarga_verificada(nodes, p2p, mp3_bytes):
    a, b = nodes
    tid = _track_id(b, "paranoid")

    async def main():
        c = p2p.client(a, b)
        path, meta = await c.download_file(tid, a.settings.fetched_dir)
        await c.aclose()
        return path, meta

    path, meta = run(main())
    assert os.path.getsize(path) == meta["file_size"]
    # el fichero descargado es EXACTAMENTE el .tmisc de B
    with open(b.catalog.resolve(tid)["path"], "rb") as fh:
        assert fh.read() == open(path, "rb").read()


def test_descarga_corrupta_se_descarta(nodes, p2p):
    """B sirve bytes que no cuadran con el hash (disco corrupto o peer mentiroso):
    el .part se borra y no queda nada indexable."""
    a, b = nodes
    tid = _track_id(b, "paranoid")
    row = b.catalog.resolve(tid)

    # corromper UN byte del audio dentro del .tmisc de B
    with open(row["path"], "r+b") as fh:
        fh.seek(row["audio_offset"] + 1000)
        byte = fh.read(1)
        fh.seek(row["audio_offset"] + 1000)
        fh.write(bytes([byte[0] ^ 0xFF]))

    async def main():
        from tmisc.peerclient import PeerError
        c = p2p.client(a, b)
        with pytest.raises(PeerError, match="no coincide"):
            await c.download_file(tid, a.settings.fetched_dir)
        await c.aclose()

    run(main())
    assert os.listdir(a.settings.fetched_dir) == []


# --- admin (app principal) -------------------------------------------------------

def test_peers_list_y_revocacion_por_http(nodes):
    a, _b = nodes
    tc = TestClient(a.app)      # TestClient llega como "testclient", no loopback

    r = tc.get("/peers")
    assert r.status_code == 200
    assert len(r.json()["peers"]) == 1

    # la mutación exige loopback: desde fuera, 403
    needle = r.json()["peers"][0]["node_id"][:12]
    assert tc.delete(f"/peers/{needle}").status_code == 403

    tc_local = TestClient(a.app, client=("127.0.0.1", 51000))
    assert tc_local.delete(f"/peers/{needle}").status_code == 200
    assert tc_local.get("/peers").json()["peers"] == []
