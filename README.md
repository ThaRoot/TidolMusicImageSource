# TMISC — tu biblioteca, tus dispositivos, tus amigos

Un nodo que sirve **tu biblioteca de música tal como está en disco** (mp3, flac,
opus, m4a, wav…) a tus dispositivos, y la comparte **P2P con amigos de confianza**
sin servidores de terceros. Ver [docs/VISION.md](docs/VISION.md).

```bash
tmisc serve --library ~/Music --host 0.0.0.0     # no convierte ni copia nada

vlc http://192.168.1.70:8787/playlist.xspf      # la biblioteca entera, con carátulas
```

Lo sirve por HTTP con `Range`, así que **VLC, mpv, el navegador o una rocola lo
reproducen con seek**. Cada pista se identifica por el hash de su audio **sin
etiquetas**: editarlas en Picard no cambia nada, y la misma canción en dos sitios
es un solo track.

Además existe el formato **`.tmisc`** (opcional): un BMP válido que lleva dentro
el audio íntegro, sus metadatos y su hash, para pasar una canción con su portada
en un solo archivo.

```bash
tmisc encode cancion.flac -c portada.jpg -o cancion.tmisc --artist "Radiohead"
```

## Qué cambió respecto de v1 (y por qué)

v1 escondía el audio en los bits menos significativos de los píxeles
(esteganografía LSB). Como formato para compartir música en local, eso era una
mala elección, y los números lo dicen:

| Modo | mp3 de 9.2 MB → `.tmisc` | Factor | ¿Seek? |
|---|---|---|---|
| `lsb` (v1, por defecto) | 95.3 MB | **10.40x** | no |
| `append` v2 con portada BMP | 9.9 MB | **1.08x** | sí |
| `append` v2 con portada BMP de 8 bits | 9.4 MB | **1.03x** | sí |
| `append` v2 con portada JPEG | 9.2 MB | **1.00x** | sí |

Y sobre todo: **en tu red local no hay nadie de quien esconder el audio.** La
esteganografía cuesta 10x de ancho de banda y hace imposible el seek para
protegerte de un adversario que no existe entre tu teléfono y tu rocola.

Lo que sí vale la pena de la idea original es la otra mitad: *un archivo que es a
la vez la portada y la canción*. Eso es lo que es v2.

El modo `lsb` sigue disponible (`-m lsb`) para quien de verdad quiera ocultar
audio, bien etiquetado como lento y pesado.

## Cómo funciona

```
┌────────────────────────────┐ 0
│ BMP (portada real)         │  se abre en cualquier visor
├────────────────────────────┤ audio_offset  ← el footer dice dónde
│ AUDIO (bytes originales)   │  mp3/flac/opus intactos, sin transcodificar
├────────────────────────────┤
│ META (JSON)                │  título, artista, duración, MBID…
├────────────────────────────┤
│ FOOTER (120 B)             │  offsets + BLAKE3 + CRC
└────────────────────────────┘
```

Servir un rango del audio es `seek(audio_offset + inicio)`. No hay que
decodificar nada, y la memoria es constante: da igual que el FLAC pese 400 MB.

Puedes comprobarlo **sin usar este código**, que es justo lo que hace que
cualquier cliente pueda implementar su propio lector:

```bash
tmisc probe cancion.tmisc          # dice audio_offset y audio_size
dd if=cancion.tmisc of=salida.mp3 bs=1 skip=750054 count=9602656
# salida.mp3 es byte a byte el original
```

La especificación binaria completa está en **[SPEC.md](SPEC.md)**.

### El id es el hash del audio sin etiquetas

`id = BLAKE3(audio sin etiquetas)`: sin ID3/APE en mp3, solo los frames en flac,
los paquetes de audio en ogg/opus, `mdat` en m4a (detalle en [SPEC.md](SPEC.md) §5).
Editar etiquetas o portada **no cambia el id**, y la misma canción suelta y en un
`.tmisc` **es el mismo track**: el catálogo y el P2P deduplican solos.

El id no cubre las etiquetas, así que no sirve para saber si un fichero llegó
intacto: por eso el P2P verifica además el hash del fichero entero.

BLAKE3 y no SHA-256 porque es un árbol de Merkle nativo: permite verificar un
fragmento contra el hash raíz sin tener el archivo entero. Es lo que hará barato
el P2P por trozos más adelante.

## Uso

### Empaquetar

```bash
tmisc encode cancion.flac -c portada.jpg -o cancion.tmisc \
      --title "Paranoid Android" --artist "Radiohead" --album "OK Computer"

tmisc encode cancion.flac --cover-container jpeg    # overhead ~1.00x
tmisc encode cancion.flac --cover-depth 8           # BMP paletizado, ~1.03x
tmisc encode cancion.flac                           # sin portada: identicon del hash
```

### Convertir una biblioteca a .tmisc (opcional)

**No hace falta para servirla**: `tmisc serve --library ~/Music` ya la sirve tal
cual. Esto solo es para quien quiera tener cada canción con su portada en un único
archivo. Lee tags y carátula embebida y crea una COPIA en `.tmisc` (ocupa el doble):

```bash
tmisc import ~/Music --library ~/tmisc-lib
```

### Servir

```bash
tmisc serve --library ~/tmisc-lib --host 0.0.0.0 --port 8787
```

| Ruta | |
|---|---|
| `/stream/{id}` | el audio, con `Range` → **seek real** |
| `/cover/{id}` · `/cover/{id}.jpg?size=300` | la portada |
| `/catalog?q=…` | el catálogo en JSON |
| `/playlist.xspf` · `/playlist.m3u8` | la cola, para VLC |
| `/track/{id}` · `/file/{id}` | metadatos · el `.tmisc` entero (P2P) |

Clientes:

```bash
vlc  http://192.168.1.70:8787/stream/<id>.mp3
mpv  http://192.168.1.70:8787/stream/<id>.flac
vlc  "http://192.168.1.70:8787/playlist.xspf?q=radiohead"
```

En el navegador es un `<audio>` normal:
```html
<audio controls src="http://192.168.1.70:8787/stream/<id>.mp3"></audio>
```

### Inspeccionar

```bash
tmisc probe cancion.tmisc --verify     # offsets, hash, metadatos, integridad
tmisc decode cancion.tmisc -o out.mp3  # recuperar el audio original
```

## P2P en la LAN

Dos PCs con nodos TMISC pueden compartir biblioteca **sin Internet y sin
servidor central**: tu reproductor siempre habla con TU nodo en `127.0.0.1`, y
si la canción vive en la otra máquina, tu nodo la trae por un canal TLS
autenticado con claves Ed25519. VLC no se entera de nada.

```bash
# en cada máquina:
tmisc serve --library ~/tmisc-lib --p2p

# emparejar (una vez): en una máquina...
tmisc pair --accept                 # muestra un código de 6 dígitos
# ...y en la otra (la IP sale de `tmisc peers discover`, o directa):
tmisc pair 192.168.1.70:8788        # muestra EL MISMO código -> confirmar en ambas

# reproducir la biblioteca de la RED entera, desde tu propio nodo:
curl http://127.0.0.1:8787/network/catalog
vlc  http://127.0.0.1:8787/stream/<id>     # da igual qué PC tenga el archivo
```

Cómo funciona por dentro:

- **Identidad**: cada nodo genera un par Ed25519 en el primer arranque
  (`tmisc id`). `node_id = BLAKE3(clave pública)` — la misma función de
  direccionamiento que usa el audio.
- **Pairing**: código corto estilo Bluetooth (commit → reveal → SAS). Un
  hombre-en-medio no puede moler el código: cada intento es un 10⁻⁶ y quema la
  ventana de `--accept`. Confirmación humana en ambos lados; solo entonces se
  persiste el peer en `peers.json`.
- **Canal**: TLS 1.3 en un puerto aparte (`puerto+1`), con el cert del peer
  **pineado** (intercambiado en el pairing). La sesión se abre con un
  challenge firmado con Ed25519 (ligado al canal: una firma capturada no sirve
  contra otro nodo). El token compartido del player jamás abre este canal.
- **Pasarela**: `GET /stream/{id}` local resuelve quién tiene el track,
  traslada el `Range` del audio al fichero remoto (`audio_offset + rango`) y
  re-sirve. En background baja el `.tmisc` completo al caché, **verificado
  contra su propio id** (el id ES el hash BLAKE3 del audio): la segunda
  reproducción es 100 % local y sobrevive a que el otro nodo se apague.
- **Descubrimiento**: mDNS (`_tmisc._tcp.local.`), con `tmisc peers discover`.
  Si la red bloquea multicast (Docker bridge, WSL2, AP isolation), todo
  funciona con direcciones explícitas: `tmisc pair host:puerto` y
  `tmisc peers addr`.
- **Revocación**: `tmisc peers rm <id|nombre>` — unilateral e inmediata (las
  sesiones vivas de ese peer caen en el acto).

Descubierto ≠ confiado: ver un nodo por mDNS no da acceso a nada. Solo el
pairing confirmado por humanos en ambas pantallas mete a alguien en
`peers.json`, y `/p2p/v1/*` devuelve 403/401 a cualquier otro.

El protocolo binario y HTTP está especificado en [SPEC.md](SPEC.md) §9 para
implementarlo desde otro lenguaje (la rocola, un ESP32 con TLS…).

## Seguridad

Por defecto escucha en `127.0.0.1`. Servir a toda la LAN es una decisión
explícita (`--host 0.0.0.0`), y puedes exigir un token:

```bash
tmisc serve --host 0.0.0.0 --token secreto
vlc "http://192.168.1.70:8787/stream/<id>.mp3?k=secreto"
```

(El token se acepta por query param además de por cabecera porque VLC no envía
cabeceras propias cuando le pasas una URL.)

Esto es **ocultación, no cifrado**: cualquiera que tenga el archivo puede sacar
el audio. Si necesitas confidencialidad, cifra el audio antes de empaquetarlo.

## Perfiles

- **`node`** (por defecto) — sirve la biblioteca: streaming, catálogo, playlists.
- **`tools`** — solo `encode` / `decode` / `probe`. **Las rutas de streaming ni
  siquiera se registran**, así que un despliegue mal configurado no puede acabar
  sirviendo audio.

## Despliegue

TMISC es un **nodo para tu casa**: el audio vive en tu máquina. Pensado para la
LAN y, fuera de casa o entre casas, para Tailscale. No lo expongas a Internet ni
lo despliegues en un VPS.

## Desarrollo

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q
```

Los fixtures de `tests/data/` son `.tmisc` **v1** generados con el código
original, para que la compatibilidad hacia atrás no se rompa en silencio.
