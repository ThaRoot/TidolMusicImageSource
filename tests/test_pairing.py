"""El pairing con código corto, de extremo a extremo y con sus ataques.

El flujo real: dos daemons, cada CLI habla con el suyo por loopback. Aquí los
dos nodos conviven en el proceso y las CLIs se simulan con TestClient contra
los endpoints /peers/pair/* de cada app principal.
"""

import time

import pytest
from fastapi.testclient import TestClient

from tmisc.pairing import compute_sas

LOOPBACK = ("127.0.0.1", 51000)


@pytest.fixture
def ab(p2p, mp3_bytes):
    """A y B SIN emparejar. B tiene un track (para verificar el final feliz)."""
    a = p2p.node("nodo-a")
    b = p2p.node("nodo-b", [(mp3_bytes, "paranoid")])
    return a, b


def _clis(a, b):
    return (TestClient(a.app, client=LOOPBACK),
            TestClient(b.app, client=LOOPBACK))


def test_pairing_completo(ab, p2p):
    a, b = ab
    cli_a, cli_b = _clis(a, b)

    # B abre la ventana; A inicia contra la dirección de B
    assert cli_b.post("/peers/pair/accept").status_code == 200
    view_a = cli_a.post("/peers/pair", json={"target": b.addr}).json()

    # ambos lados muestran EL MISMO código
    view_b = cli_b.get("/peers/pair/incoming").json()["session"]
    assert view_a["sas"] is not None
    assert view_a["sas"] == view_b["sas"]
    assert len(view_a["sas"]) == 6
    assert view_a["sid"] == view_b["sid"]
    # y cada uno ve la identidad del otro
    assert view_a["peer"]["node_id"] == b.identity.node_id
    assert view_b["peer"]["node_id"] == a.identity.node_id

    sid = view_a["sid"]

    # A confirma primero: queda a la espera de B
    assert cli_a.post(f"/peers/pair/{sid}/confirm").json()["state"] != "done"
    assert a.peers.get(b.identity.node_id) is None       # nada persiste aún

    # B confirma; el siguiente poll de A empuja su confirmación y cierra
    cli_b.post(f"/peers/pair/{sid}/confirm")
    final_a = cli_a.get(f"/peers/pair/{sid}").json()

    assert final_a["state"] == "done"
    assert cli_b.get("/peers/pair/incoming").json()["session"]["state"] == "done"
    # AHORA sí: cada nodo confía en el otro, con clave y cert
    assert a.peers.get(b.identity.node_id).cert_pem == b.identity.cert_pem
    assert b.peers.get(a.identity.node_id).pubkey_hex == a.identity.pubkey_hex


def test_pairing_da_acceso_real(ab, p2p):
    """Tras el pairing, A puede abrir sesión y leer el catálogo de B."""
    import asyncio

    a, b = ab
    cli_a, cli_b = _clis(a, b)

    cli_b.post("/peers/pair/accept")
    sid = cli_a.post("/peers/pair", json={"target": b.addr}).json()["sid"]
    cli_b.post(f"/peers/pair/{sid}/confirm")
    cli_a.post(f"/peers/pair/{sid}/confirm")
    assert cli_a.get(f"/peers/pair/{sid}").json()["state"] == "done"

    async def main():
        c = p2p.client(a, b)
        items = await c.catalog()
        await c.aclose()
        return items

    assert [i["title"] for i in asyncio.run(main())] == ["paranoid"]


def test_sin_ventana_accept_403(ab):
    a, b = ab
    cli_a, _ = _clis(a, b)
    r = cli_a.post("/peers/pair", json={"target": b.addr})
    assert r.status_code == 502
    assert "accept" in r.json()["detail"]


def test_la_ventana_se_consume_con_un_intento(ab, p2p):
    a, b = ab
    cli_a, cli_b = _clis(a, b)
    cli_b.post("/peers/pair/accept")
    cli_a.post("/peers/pair", json={"target": b.addr})

    # segundo intento dentro de la misma ventana: rechazado
    intruso = p2p.node("intruso")
    cli_i = TestClient(intruso.app, client=LOOPBACK)
    r = cli_i.post("/peers/pair", json={"target": b.addr})
    assert r.status_code == 502


def test_commit_roto_aborta(ab):
    """Un iniciador que revela un nonce distinto del que comprometió está
    intentando moler el SAS: la sesión muere ahí."""
    a, b = ab
    tc_b = TestClient(b.peer_app, client=LOOPBACK)
    b.pairing.open_accept()

    from blake3 import blake3
    start = tc_b.post("/p2p/v1/pair/start", json={
        "node_id": a.identity.node_id,
        "pubkey": a.identity.pubkey_hex,
        "cert_pem": a.identity.cert_pem,
        "name": "nodo-a",
        "peer_port": 9999,
        "commit": blake3(b"\x01" * 32).hexdigest(),
    })
    assert start.status_code == 200
    sid = start.json()["session_id"]

    r = tc_b.post("/p2p/v1/pair/reveal", json={
        "session_id": sid, "nonce": ("02" * 32),         # != commit
    })
    assert r.status_code == 400
    # la sesión quedó quemada: ni siquiera el nonce bueno la revive
    r = tc_b.post("/p2p/v1/pair/reveal", json={
        "session_id": sid, "nonce": ("01" * 32),
    })
    assert r.status_code == 400


def test_identidad_incoherente_rechazada(ab, p2p):
    """Anunciar la clave de OTRO nodo con un cert propio no pasa del start."""
    a, b = ab
    otro = p2p.node("otro")
    tc_b = TestClient(b.peer_app, client=LOOPBACK)
    b.pairing.open_accept()

    from blake3 import blake3
    r = tc_b.post("/p2p/v1/pair/start", json={
        "node_id": otro.identity.node_id,
        "pubkey": otro.identity.pubkey_hex,       # clave de 'otro'...
        "cert_pem": a.identity.cert_pem,          # ...cert de A
        "name": "suplantador",
        "peer_port": 9999,
        "commit": blake3(b"\x03" * 32).hexdigest(),
    })
    assert r.status_code == 400


def test_mitm_produce_sas_distintos():
    """Dos sesiones con claves distintas no comparten SAS: es lo que el humano
    detecta al comparar pantallas."""
    pk_a, pk_b, pk_m = "aa" * 32, "bb" * 32, "cc" * 32
    n1, n2 = "11" * 32, "22" * 32
    cert = "-----BEGIN CERTIFICATE-----\nfalso\n-----END CERTIFICATE-----\n"

    sas_ab = compute_sas(pk_a, pk_b, n1, n2, cert)
    sas_am = compute_sas(pk_a, pk_m, n1, n2, cert)     # M suplantando a B
    sas_mb = compute_sas(pk_m, pk_b, n1, n2, cert)     # M suplantando a A
    assert sas_ab != sas_am and sas_ab != sas_mb


def test_rechazo_del_usuario_no_persiste_nada(ab):
    a, b = ab
    cli_a, cli_b = _clis(a, b)
    cli_b.post("/peers/pair/accept")
    sid = cli_a.post("/peers/pair", json={"target": b.addr}).json()["sid"]

    # B confirma pero A NO (el humano de A vio un código distinto y cortó)
    cli_b.post(f"/peers/pair/{sid}/confirm")

    assert a.peers.all() == []
    assert b.peers.all() == []


def test_sesion_caducada_no_confirma(ab):
    a, b = ab
    cli_a, cli_b = _clis(a, b)
    cli_b.post("/peers/pair/accept")
    sid = cli_a.post("/peers/pair", json={"target": b.addr}).json()["sid"]

    a.pairing.current().expires_at = time.monotonic() - 1   # caducidad simulada
    assert cli_a.post(f"/peers/pair/{sid}/confirm").status_code == 409
    assert cli_a.get(f"/peers/pair/{sid}").json()["state"] == "failed"
