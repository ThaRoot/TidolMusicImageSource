"""CLI de TMISC.

  tmisc encode  audio.flac -c portada.jpg -o salida.tmisc --title "..." --artist "..."
  tmisc decode  salida.tmisc -o recuperado.flac
  tmisc probe   salida.tmisc          # offsets y hash, sin extraer el audio
  tmisc import  ~/Music --library ~/tmisc-lib
  tmisc scan    --library ~/tmisc-lib
  tmisc serve   --library ~/tmisc-lib --host 0.0.0.0
  tmisc migrate viejo.tmisc           # v1 -> v2, sin recomprimir el audio
"""

import argparse
import os
import sys

from tmisc import decode as dec
from tmisc import encode as enc
from tmisc.catalog import Catalog
from tmisc.config import PROFILE_NODE, PROFILE_TOOLS, Settings
from tmisc.container import iter_audio_slice, probe, read_meta
from tmisc.errors import TmiscError
from tmisc.meta import AudioMeta, IdsMeta, TrackMeta


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def cmd_encode(a) -> None:
    cover = None
    if a.cover:
        with open(a.cover, "rb") as fh:
            cover = fh.read()

    audio_meta, tags = enc.probe_audio_tags(a.audio)
    track = TrackMeta(
        title=a.title or tags.title,
        artist=a.artist or tags.artist,
        album=a.album or tags.album,
        album_artist=tags.album_artist,
        track_no=tags.track_no, disc_no=tags.disc_no,
        year=tags.year, genre=tags.genre, isrc=tags.isrc,
    )

    out = a.output or os.path.splitext(a.audio)[0] + ".tmisc"
    size = os.path.getsize(a.audio)

    if a.mode == "lsb":
        from PIL import Image
        from tmisc.cover import load_cover
        from tmisc.legacy_lsb import encode_lsb, generate_cover_for_lsb
        from tmisc.sniff import safe_ext

        with open(a.audio, "rb") as fh:
            audio = fh.read()
        img: Image.Image = load_cover(cover) if cover else generate_cover_for_lsb(audio)
        data = encode_lsb(img, audio, safe_ext(a.audio.rsplit(".", 1)[-1]))
        with open(out, "wb") as fh:
            fh.write(data)
        print(f"OK  modo=lsb  {_human(size)} -> {_human(len(data))} "
              f"({len(data)/size:.2f}x)  {out}")
        print("    aviso: LSB no permite seek y pesa ~10x. Usa append salvo que "
              "necesites esconder el audio de verdad.")
        return

    with open(a.audio, "rb") as fh:
        footer = enc.encode_to_file(
            fh, size, out,
            cover_data=cover, audio_meta=audio_meta, track=track,
            ids=IdsMeta(recording_mbid=a.mbid),
            source_filename=os.path.basename(a.audio),
            side=a.cover_side, depth=a.cover_depth, container=a.cover_container,
        )

    ratio = footer.file_size / size
    print(f"OK  modo=append-v2  {_human(size)} -> {_human(footer.file_size)} ({ratio:.2f}x)  {out}")
    print(f"    id (blake3 del audio) : {footer.track_id}")
    print(f"    audio_offset          : {footer.audio_offset}")
    if track.title:
        print(f"    {track.artist or '?'} — {track.title}")


def cmd_decode(a) -> None:
    info, chunks = dec.decode_file(a.file)
    out = a.output or f"recovered.{info.ext}"
    with open(out, "wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
    print(f"OK  modo={dec.mode_name(info)}  formato={info.ext}  "
          f"-> {out} ({_human(info.audio_size)})")


def cmd_probe(a) -> None:
    info = probe(a.file)
    meta = read_meta(info)

    print(f"archivo      : {a.file}")
    print(f"versión      : v{info.version}   modo: {dec.mode_name(info)}")
    print(f"streameable  : {'sí (slice directo)' if info.streamable else 'no (LSB: hay que decodificar)'}")
    print(f"tamaño total : {_human(info.file_size)}")
    print(f"  imagen     : {_human(info.image_size)}  [0, {info.image_size})")
    print(f"  audio      : {_human(info.audio_size)}  [{info.audio_offset}, "
          f"{info.audio_offset + info.audio_size})")
    if info.audio_size:
        print(f"overhead     : {info.file_size / info.audio_size:.3f}x")
    print(f"mime         : {info.mime}   ext: {info.ext}")
    print(f"id           : {info.track_id or '(v1: se calcula al indexar)'}")

    if meta:
        t = meta.track
        print(f"título       : {t.title or '-'}")
        print(f"artista      : {t.artist or '-'}")
        print(f"álbum        : {t.album or '-'}")
        if meta.audio.duration_ms:
            secs = meta.audio.duration_ms / 1000
            print(f"duración     : {int(secs // 60)}:{int(secs % 60):02d}")

    if a.verify:
        dec.verify(info)
        print("verificación : OK, el audio coincide con el hash del footer")


def cmd_scan(a) -> None:
    settings = _settings_from_args(a)
    settings.ensure_dirs()
    cat = Catalog(settings.db_path)
    from tmisc.scanner import scan

    stats = scan(settings.scan_roots, cat)
    print(f"escaneados {stats.scanned} · nuevos {stats.added} · sin cambios "
          f"{stats.skipped} · eliminados {stats.removed} · fallidos {stats.failed}")
    for err in stats.errors:
        print(f"  ! {err}", file=sys.stderr)
    print(f"catálogo: {cat.count()} tracks en {settings.db_path}")


def cmd_import(a) -> None:
    settings = _settings_from_args(a)
    settings.ensure_dirs()
    from tmisc.importer import import_tree
    from tmisc.scanner import scan

    summary = import_tree(a.path, settings, recursive=not a.no_recursive)
    print(f"encontrados {summary['found']} · importados {summary['imported']} "
          f"· fallidos {summary['failed']}")
    for err in summary["errors"]:
        print(f"  ! {err}", file=sys.stderr)

    cat = Catalog(settings.db_path)
    scan(settings.scan_roots, cat)
    print(f"catálogo: {cat.count()} tracks")


def cmd_migrate(a) -> None:
    """v1 -> v2 reutilizando el audio tal cual: no se recomprime nada."""
    info = probe(a.file)
    if info.version == 2:
        print("Ya es v2; nada que hacer.")
        return

    out = a.output or a.file
    tmp = out + ".migrating"
    cover = None
    if info.streamable and info.image_size:
        with open(info.file if hasattr(info, "file") else a.file, "rb") as fh:
            cover = fh.read(info.image_size)

    audio = b"".join(iter_audio_slice(info)) if info.streamable else dec.decode_bytes(
        open(a.file, "rb").read()
    )[0]

    import io
    footer = enc.encode_to_file(
        io.BytesIO(audio), len(audio), tmp,
        cover_data=cover, track=TrackMeta(), audio_meta=AudioMeta(),
        source_filename=os.path.basename(a.file),
    )
    os.replace(tmp, out)
    print(f"OK  v{info.version} -> v2  id={footer.track_id[:16]}…  {out}")


def cmd_serve(a) -> None:
    import uvicorn

    settings = _settings_from_args(a)
    settings.host = a.host
    settings.port = a.port
    settings.profile = a.profile
    settings.token = a.token or settings.token
    settings.p2p = a.p2p or settings.p2p
    if a.peer_port:
        settings.peer_port = a.peer_port
    if a.no_mdns:
        settings.mdns = False
    settings.ensure_dirs()

    cat = None
    if settings.serves_audio:
        cat = Catalog(settings.db_path)
        from tmisc.scanner import scan
        stats = scan(settings.scan_roots, cat)
        print(f"biblioteca : {settings.library}")
        print(f"catálogo   : {cat.count()} tracks "
              f"(nuevos {stats.added}, fallidos {stats.failed})")

    if a.host == "0.0.0.0" and not settings.token:
        print("aviso: sirviendo a toda la LAN sin token. Usa --token para restringirlo.")

    from server.app import create_app
    print(f"perfil     : {settings.profile}")
    print(f"escuchando : http://{a.host}:{a.port}")
    if settings.serves_audio:
        print(f"  vlc http://{a.host}:{a.port}/playlist.xspf")

    if not (settings.p2p and settings.serves_audio):
        uvicorn.run(create_app(settings, catalog=cat), host=a.host, port=a.port,
                    log_level="info")
        return

    # --- modo P2P: dos listeners en el mismo event loop ----------------------
    # El player sigue en HTTP plano (host:port). El canal entre nodos es OTRO
    # puerto, TLS con el cert del nodo, y solo entienden de sesiones de peer.
    import asyncio

    from server.app import create_peer_app
    from tmisc.identity import NodeIdentity
    from tmisc.pairing import PairingManager
    from tmisc.peers import PeerStore, SessionManager

    from tmisc.peerclient import PeerNetwork

    identity = NodeIdentity.load_or_create(settings.state_dir)
    peers = PeerStore(settings.state_dir)
    sessions = SessionManager(identity, peers)
    pairing = PairingManager(identity, peers, settings.node_name,
                             settings.effective_peer_port)
    network = PeerNetwork(identity, peers, cat, settings)

    app = create_app(settings, catalog=cat, identity=identity, peers=peers,
                     sessions=sessions, network=network, pairing=pairing)
    peer_app = create_peer_app(settings, identity, peers, cat,
                               sessions=sessions, pairing=pairing)

    print(f"node_id    : {identity.node_id}")
    print(f"peers      : {len(peers.all())} emparejados"
          + ("" if peers.all() else "  (usa `tmisc pair` para añadir)"))
    print(f"p2p        : https://{settings.peer_host}:{settings.effective_peer_port} "
          f"(TLS, solo peers emparejados)")

    async def run_both() -> None:
        discovery = None
        if settings.mdns:
            try:
                from tmisc.discovery import Discovery
                discovery = Discovery(identity, settings, peers=peers, network=network)
                await discovery.start()
                app.state.discovery = discovery
                print(f"mDNS       : anunciando _tmisc._tcp.local. como "
                      f"'{settings.node_name}'")
            except Exception as e:
                # multicast bloqueado (Docker bridge, WSL2, AP isolation):
                # el P2P sigue funcionando con direcciones explícitas.
                print(f"aviso: mDNS no disponible ({e}); usa `tmisc pair host:puerto`")
                discovery = None

        main_srv = uvicorn.Server(uvicorn.Config(
            app, host=a.host, port=a.port, log_level="info"))
        peer_srv = uvicorn.Server(uvicorn.Config(
            peer_app, host=settings.peer_host, port=settings.effective_peer_port,
            ssl_certfile=identity.cert_path, ssl_keyfile=identity.key_path,
            log_level="warning"))

        tasks = [asyncio.create_task(main_srv.serve()),
                 asyncio.create_task(peer_srv.serve())]
        try:
            # Ctrl+C lo captura uno de los dos (el último en instalar handlers);
            # cuando uno cae, se apaga el otro y se espera el shutdown limpio.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            main_srv.should_exit = True
            peer_srv.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if discovery is not None:
                await discovery.stop()

    asyncio.run(run_both())


def cmd_pair(a) -> None:
    """Empareja con otro nodo comparando un código de 6 dígitos en ambas pantallas.

    La CLI es solo la UI: el protocolo lo ejecuta el daemon local (`serve --p2p`).
    """
    import time as _time

    import httpx

    settings = _settings_from_args(a)
    if a.port:
        settings.port = a.port
    base = f"http://127.0.0.1:{settings.port}"
    params = {"k": settings.token} if settings.token else None

    def fail(msg: str) -> None:
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    with httpx.Client(base_url=base, params=params, timeout=10.0) as c:
        try:
            c.get("/health").raise_for_status()
        except httpx.HTTPError:
            fail(f"no hay un nodo corriendo en {base}. Arranca `tmisc serve --p2p` primero.")

        if a.accept:
            r = c.post("/peers/pair/accept")
            if r.status_code != 200:
                fail(r.json().get("detail", r.text))
            print("aceptando UN pairing durante 120 s.")
            print("en el otro nodo:  tmisc pair <ip-de-esta-máquina>:<puerto-p2p>")
            session = None
            deadline = _time.time() + 125
            while _time.time() < deadline:
                session = c.get("/peers/pair/incoming").json()["session"]
                if session and session.get("sas"):
                    break
                _time.sleep(1)
            if not session or not session.get("sas"):
                fail("nadie inició el pairing dentro de la ventana.")
        else:
            if not a.target:
                fail("falta el destino: tmisc pair <host:puerto-p2p>   (o usa --accept)")
            r = c.post("/peers/pair", json={"target": a.target})
            if r.status_code != 200:
                fail(r.json().get("detail", r.text))
            session = r.json()

        sid, peer = session["sid"], session["peer"]
        print()
        print(f"  nodo remoto : {peer['name'] or '(sin nombre)'}  {peer['node_id'][:16]}…")
        print(f"  código      : {session['sas']}")
        print()
        answer = input("¿El código coincide en las DOS pantallas? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            print("cancelado; no se confió en nadie.")
            return

        r = c.post(f"/peers/pair/{sid}/confirm")
        if r.status_code != 200:
            fail(r.json().get("detail", r.text))

        deadline = _time.time() + 125
        while _time.time() < deadline:
            view = c.get(f"/peers/pair/{sid}").json()
            if view["state"] == "done":
                print(f"emparejado ✓  {peer['name']} ({peer['node_id'][:16]}…)")
                return
            if view["state"] == "failed":
                fail("el pairing caducó o falló; vuelve a intentarlo.")
            _time.sleep(1)
        fail("el otro lado no confirmó a tiempo.")


def cmd_peers(a) -> None:
    from tmisc.peers import PeerStore

    settings = _settings_from_args(a)

    if a.action == "discover":
        import asyncio

        from tmisc.discovery import browse
        from tmisc.identity import NodeIdentity

        ident = NodeIdentity.load_or_create(settings.state_dir)
        store = PeerStore(settings.state_dir)
        print("buscando nodos TMISC en la LAN (3 s)…")
        found = asyncio.run(browse(ident, settings, seconds=3.0))
        if not found:
            print("ninguno. Si la red bloquea multicast (Docker, WSL2, AP "
                  "isolation), usa `tmisc pair host:puerto` con la IP directa.")
            return
        for nid, e in found.items():
            estado = "emparejado" if store.get(nid) else "sin emparejar"
            print(f"{e['name'] or '(sin nombre)':<20} {nid[:16]}…  "
                  f"{e['host']}:{e['port']}  [{estado}]")
        return

    if a.action == "addr":
        if not a.target or not a.value:
            print("uso: tmisc peers addr <id|nombre> <host:puerto>", file=sys.stderr)
            sys.exit(2)
        store = PeerStore(settings.state_dir)
        peer = store.match(a.target)
        if peer is None:
            print("ningún peer coincide.", file=sys.stderr)
            sys.exit(1)
        store.update_addr(peer.node_id, a.value)
        print(f"{peer.node_id[:16]}…  ->  {a.value}")
        return

    if a.action == "list":
        store = PeerStore(settings.state_dir)
        if not store.all():
            print("sin peers emparejados. Usa `tmisc pair` con el otro nodo.")
            return
        for p in store.all():
            print(f"{p.node_id[:16]}…  {p.name or '(sin nombre)':<20}  {p.addr or '-'}")
        return

    if a.action == "rm":
        if not a.target:
            print("Error: falta el peer (prefijo del node_id o nombre).", file=sys.stderr)
            sys.exit(2)
        # Primero el daemon local: además de persistir, invalida las sesiones
        # vivas de ese peer. Sin daemon, se edita peers.json directamente.
        import httpx
        url = f"http://127.0.0.1:{settings.port}/peers/{a.target}"
        params = {"k": settings.token} if settings.token else None
        try:
            r = httpx.delete(url, params=params, timeout=3.0)
            if r.status_code == 200:
                print(f"revocado: {r.json()['removed'][:16]}…")
                return
            if r.status_code == 404:
                print("ningún peer coincide.", file=sys.stderr)
                sys.exit(1)
            print(f"el daemon respondió HTTP {r.status_code}: {r.text}", file=sys.stderr)
            sys.exit(1)
        except httpx.HTTPError:
            store = PeerStore(settings.state_dir)
            peer = store.match(a.target)
            if peer is None:
                print("ningún peer coincide.", file=sys.stderr)
                sys.exit(1)
            store.remove(peer.node_id)
            print(f"revocado: {peer.node_id[:16]}…  (sin daemon: si hay un nodo "
                  f"corriendo, sus sesiones vivas duran hasta reiniciarlo)")
        return


def cmd_fetch(a) -> None:
    """Trae un track de la red al caché local, verificado (vía el daemon)."""
    import time as _time

    import httpx

    settings = _settings_from_args(a)
    if a.port:
        settings.port = a.port
    base = f"http://127.0.0.1:{settings.port}"
    params = {"k": settings.token} if settings.token else None

    with httpx.Client(base_url=base, params=params, timeout=10.0) as c:
        try:
            r = c.post(f"/fetch/{a.track_id}")
        except httpx.HTTPError:
            print(f"Error: no hay un nodo corriendo en {base}.", file=sys.stderr)
            sys.exit(1)
        if r.status_code == 404:
            print("ningún peer tiene ese track.", file=sys.stderr)
            sys.exit(1)
        body = r.json()
        if body.get("state") == "already-local":
            print("ya es local; nada que traer.")
            return
        job = body["job_id"]
        while True:
            j = c.get(f"/jobs/{job}").json()
            if j["state"] == "done":
                print("descargado y verificado ✓ (ya es local)")
                return
            if j["state"] == "error":
                print(f"falló: {j.get('error', 'descarga no verificada')}", file=sys.stderr)
                sys.exit(1)
            _time.sleep(0.5)


def cmd_id(a) -> None:
    """Identidad del nodo. La crea si no existe (primer arranque)."""
    from tmisc.identity import NodeIdentity

    settings = _settings_from_args(a)
    ident = NodeIdentity.load_or_create(settings.state_dir)
    print(f"node_id : {ident.node_id}")
    print(f"pubkey  : {ident.pubkey_hex}")
    print(f"estado  : {settings.state_dir}")
    print("nota    : borrar ese directorio re-clavea el nodo y rompe los pairings.")


def _settings_from_args(a) -> Settings:
    s = Settings.from_env()
    if getattr(a, "library", None):
        s.library = a.library
    if getattr(a, "cache", None):
        s.cache_dir = a.cache
    return s.resolved()


def main() -> None:
    p = argparse.ArgumentParser(prog="tmisc", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="audio -> .tmisc")
    e.add_argument("audio")
    e.add_argument("-o", "--output")
    e.add_argument("-c", "--cover")
    e.add_argument("-m", "--mode", default="append", choices=["append", "lsb"])
    e.add_argument("--title")
    e.add_argument("--artist")
    e.add_argument("--album")
    e.add_argument("--mbid", help="MusicBrainz recording id (el MBID que escribe Picard)")
    e.add_argument("--cover-side", type=int, default=500)
    e.add_argument("--cover-depth", type=int, default=24, choices=[8, 24])
    e.add_argument("--cover-container", default="bmp", choices=["bmp", "png", "jpeg"])
    e.set_defaults(func=cmd_encode)

    d = sub.add_parser("decode", help=".tmisc -> audio original")
    d.add_argument("file")
    d.add_argument("-o", "--output")
    d.set_defaults(func=cmd_decode)

    i = sub.add_parser("probe", help="offsets, hash y metadatos sin extraer el audio")
    i.add_argument("file")
    i.add_argument("--verify", action="store_true", help="comprobar el audio contra el hash")
    i.set_defaults(func=cmd_probe)
    sub.add_parser("info", parents=[i], add_help=False, conflict_handler="resolve")

    s = sub.add_parser("scan", help="indexar la biblioteca")
    s.add_argument("--library")
    s.add_argument("--cache")
    s.set_defaults(func=cmd_scan)

    im = sub.add_parser("import", help="audio suelto del disco -> .tmisc en la biblioteca")
    im.add_argument("path")
    im.add_argument("--library")
    im.add_argument("--cache")
    im.add_argument("--no-recursive", action="store_true")
    im.set_defaults(func=cmd_import)

    m = sub.add_parser("migrate", help="v1 -> v2 sin recomprimir")
    m.add_argument("file")
    m.add_argument("-o", "--output")
    m.set_defaults(func=cmd_migrate)

    nid = sub.add_parser("id", help="identidad Ed25519 del nodo (la crea si no existe)")
    nid.add_argument("--cache")
    nid.set_defaults(func=cmd_id)

    pa = sub.add_parser("pair", help="emparejar con otro nodo (código corto en ambas pantallas)")
    pa.add_argument("target", nargs="?", help="host:puerto-p2p del otro nodo")
    pa.add_argument("--accept", action="store_true", help="aceptar un pairing entrante")
    pa.add_argument("--port", type=int, default=0,
                    help="puerto del daemon local (default: 8787 o TMISC_PORT)")
    pa.add_argument("--cache")
    pa.set_defaults(func=cmd_pair)

    pr = sub.add_parser("peers", help="peers: list | rm <peer> | discover | addr <peer> <host:puerto>")
    pr.add_argument("action", choices=["list", "rm", "discover", "addr"])
    pr.add_argument("target", nargs="?")
    pr.add_argument("value", nargs="?")
    pr.add_argument("--cache")
    pr.set_defaults(func=cmd_peers)

    ft = sub.add_parser("fetch", help="traer un track de la red al caché local")
    ft.add_argument("track_id")
    ft.add_argument("--port", type=int, default=0)
    ft.add_argument("--cache")
    ft.set_defaults(func=cmd_fetch)

    sv = sub.add_parser("serve", help="gateway HTTP con Range (VLC, la rocola, el navegador)")
    sv.add_argument("--library")
    sv.add_argument("--cache")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--profile", default=PROFILE_NODE, choices=[PROFILE_NODE, PROFILE_TOOLS])
    sv.add_argument("--token")
    sv.add_argument("--p2p", action="store_true",
                    help="listener P2P (TLS) para compartir con peers emparejados")
    sv.add_argument("--peer-port", type=int, default=0, help="default: puerto+1")
    sv.add_argument("--no-mdns", action="store_true",
                    help="no anunciar el nodo por mDNS")
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args()
    try:
        args.func(args)
    except TmiscError as ex:
        print(f"Error: {ex}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as ex:
        print(f"Error: no existe {ex.filename}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
