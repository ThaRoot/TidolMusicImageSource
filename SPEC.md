# Especificación de `.tmisc` v2

Documento normativo. Si vas a implementar un lector en otro lenguaje (Rust, C,
Go, Kotlin… por ejemplo para la rocola), esto es lo único que necesitas leer.

Un `.tmisc` es **un archivo de imagen válido** que lleva dentro un archivo de
audio íntegro, sus metadatos y su hash. Se abre en cualquier visor de imágenes y
suena en cualquier reproductor que sepa leer un rango de bytes.

## 1. Estructura

```
┌────────────────────────────────┐ offset 0
│ IMAGEN   (BMP por defecto)     │  image_size bytes
├────────────────────────────────┤ audio_offset
│ AUDIO    (bytes originales,    │  audio_size bytes
│           sin transcodificar)  │
├────────────────────────────────┤ meta_offset
│ META     (JSON UTF-8)          │  meta_size bytes
├────────────────────────────────┤ file_size - 120
│ FOOTER   (112 B + 8 B)         │  120 bytes
└────────────────────────────────┘ EOF
```

El audio va **antes** que los metadatos a propósito. Los metadatos son la parte
mutable (retaggear, añadir un MBID, añadir la letra) y el audio es inmutable —
es la dirección de contenido. Con este orden, retaggear significa truncar en
`meta_offset` y reescribir JSON + footer: el audio no se mueve, `audio_offset` no
cambia, el hash no cambia y por tanto **el id del track no cambia**.

## 2. Por qué sigue siendo una imagen válida

Un lector de BMP localiza los píxeles con `bfOffBits` (offset 10) y calcula
cuántos hay con `biWidth × biHeight × biBitCount`. **Nunca** usa el tamaño físico
del archivo. Todo lo que venga después del último píxel es *trailing data* y se
ignora — el mismo truco que un tag ID3v1 al final de un MP3.

`bfSize` (offset 2, u32 **little-endian**) se deja igual a `image_size`, no al
tamaño del `.tmisc`. Así el BMP es internamente correcto según su propia
especificación. Efecto secundario útil: **`bfSize` te da `image_size` gratis**,
sin parsear el DIB, así que extraer la portada es leer `[0, bfSize)`.

PNG (que termina en `IEND`) y JPEG (que termina en `FFD9`) toleran igual los datos
anexados; el campo `container` del footer indica cuál se usó.

## 3. Footer

**El footer es big-endian.** La imagen que lo precede es little-endian porque lo
manda su formato. No heredes el endianness del contenedor: es el error número uno
al implementar un lector.

### 3.1 Body — 112 bytes

| Offset | Tam | Campo | Tipo | Descripción |
|---:|---:|---|---|---|
| 0 | 1 | `version_major` | u8 | `2` |
| 1 | 1 | `version_minor` | u8 | `0` |
| 2 | 1 | `mode` | u8 | ver §3.3 |
| 3 | 1 | `flags` | u8 | ver §3.4 |
| 4 | 1 | `container` | u8 | `1`=BMP · `2`=PNG · `3`=JPEG |
| 5 | 3 | — | — | relleno, ceros |
| 8 | 8 | `file_size` | u64 | tamaño total esperado. Detecta truncamiento en O(1) |
| 16 | 8 | `image_size` | u64 | longitud de la imagen |
| 24 | 8 | **`audio_offset`** | u64 | **offset absoluto del primer byte de audio** |
| 32 | 8 | **`audio_size`** | u64 | longitud del audio |
| 40 | 8 | `meta_offset` | u64 | offset absoluto del JSON |
| 48 | 4 | `meta_size` | u32 | longitud del JSON (`0` = no hay) |
| 52 | 4 | `audio_crc32` | u32 | CRC32 del audio |
| 56 | 32 | **`audio_blake3`** | u8[32] | **BLAKE3-256 del audio = el id del track** |
| 88 | 4 | `meta_crc32` | u32 | CRC32 del JSON |
| 92 | 8 | `created_unix` | u64 | epoch de creación |
| 100 | 8 | `reserved1` | u8[8] | ceros (futuro: índice de seek) |
| 108 | 4 | `footer_crc32` | u32 | CRC32 de `body[0..108]` |

Formato `struct` de Python: `>BBBBB3xQQQQQII32sIQ8sI`

### 3.2 Tail — 8 bytes

| Offset | Tam | Campo | Valor |
|---:|---:|---|---|
| 112 | 6 | `magic` | `"TMISC2"` (ASCII) |
| 118 | 2 | `footer_size` | `120` (u16) |

Los **últimos 8 bytes del archivo son siempre** el magic y el tamaño del footer.
El footer es por tanto **auto-localizable**: una versión futura con un footer más
grande seguirá siendo localizable por un lector antiguo. Es el mismo patrón que
el EOCD de un ZIP, sin su complejidad.

### 3.3 `mode`

| Valor | Significado |
|---|---|
| 1 | LSB v1 (legacy) |
| 2 | append v1 (legacy) |
| 3 | **append v2** — el modo normal |
| 4 | LSB v2 |

### 3.4 `flags`

| Bit | Nombre | Significado |
|---|---|---|
| `0x01` | `ENCRYPTED` | reservado; en v2.0 siempre 0 |
| `0x02` | `HAS_META` | hay segmento de metadatos |
| `0x04` | `LSB_PAYLOAD` | el audio está en los píxeles: **no es un slice, no se puede streamear** |
| `0x08` | `AUDIO_TRANSCODED` | el audio no es byte-idéntico al fichero original |
| `0x10` | `HAS_SEEK_INDEX` | reservado |

## 4. Cómo leerlo (el algoritmo completo)

```
size = stat(archivo).size
if size < 14: no es un .tmisc

leer los últimos 8 bytes -> (magic, footer_size)

si magic == "TMISC2":
    leer los últimos footer_size bytes
    comprobar footer_crc32 CONTRA body[0..108]   <-- ANTES de creer ningún offset
    comprobar file_size == size                  <-- ¿truncado? ¿aún copiándose?
    -> audio en [audio_offset, audio_offset + audio_size)

si no, leer los últimos 27 bytes -> trailer v1: >6sB8sQI
si magic == "TMISC1" y mode == 2:
    audio_offset = size - 27 - audio_size
    -> mismo tratamiento; los .tmisc v1 se sirven sin migrar
```

**Valida el `footer_crc32` antes de usar ningún offset.** Un footer corrupto o
manipulado no debe poder hacer que tu lector devuelva bytes de la imagen, del
propio footer, o de fuera del archivo.

Extraer el audio es entonces: `seek(audio_offset)` y leer `audio_size` bytes.
Nada más. Compruébalo sin ninguna librería:

```bash
dd if=cancion.tmisc of=salida.mp3 bs=1 skip=$AUDIO_OFFSET count=$AUDIO_SIZE
```

## 5. Identidad del contenido

**`id = BLAKE3-256(audio sin etiquetas)`**, en hexadecimal. Esquema
**`b3-untagged-1`** (lo anuncian `/.well-known/tmisc.json` y `/p2p/v1/ping` como
`id_scheme`; dos nodos con esquemas distintos no comparten ids).

"Audio sin etiquetas" según el formato del audio (dentro de un `.tmisc`, el del
audio embebido; suelto en disco, el del fichero):

| Formato | Qué se hashea |
| :--- | :--- |
| mp3 / aac | todo menos ID3v2 (inicio) e ID3v1 / APEv2 / Lyrics3v2 (final) |
| flac | solo los frames: tras `fLaC` y el último bloque de metadatos |
| ogg / opus | el contenido de los paquetes de audio del primer stream lógico (no las cabeceras de página, que cambian al reescribir tags) |
| m4a / mp4 | el contenido de los átomos `mdat` |
| wav / aiff | el chunk `data` / `SSND` |
| otro | el audio entero; el id es **débil** (editar tags lo cambia) |

Consecuencias:
- Editar etiquetas o la portada embebida **no cambia el id**.
- La misma canción suelta y dentro de un `.tmisc` (con cualquier portada) es el
  **mismo track**: el catálogo y el P2P deduplican solos.
- El id **no garantiza la integridad del fichero** (un byte corrupto dentro de
  las etiquetas no lo cambia). Para eso, `/p2p/v1/track` anuncia `file_b3`
  (BLAKE3 del fichero entero) y quien descarga comprueba **las dos cosas**.
- El campo `audio_blake3` del footer v2 sigue siendo lo que era: integridad del
  slice de audio *con* sus etiquetas. No es el id.

BLAKE3 y no SHA-256 porque BLAKE3 es un **árbol de Merkle nativo**: permite
verificar un fragmento arbitrario contra el hash raíz sin tener el archivo
entero. Eso es justo lo que necesita una descarga P2P por trozos desde varios
peers — lo que BitTorrent v2 tuvo que construirse a mano.

## 6. Metadatos

JSON UTF-8, sin BOM. Serialización **canónica** (claves ordenadas, separadores
compactos) para que `meta_crc32` sea reproducible.

```json
{
  "tmisc": 2,
  "id": "55e8816d…",
  "audio": {
    "mime": "audio/mpeg", "codec": "mp3", "ext": "mp3",
    "size": 9602656, "duration_ms": 240039,
    "bitrate_kbps": 320, "sample_rate": 44100, "channels": 2, "lossless": false
  },
  "track": {
    "title": "Paranoid Android", "artist": "Radiohead",
    "album": "OK Computer", "track_no": 2, "year": 1997, "genre": []
  },
  "ids": { "recording_mbid": null, "release_mbid": null, "tidol_track_id": null },
  "cover": { "present": true, "width": 500, "height": 500, "container": "bmp" },
  "provenance": { "created_at": "2026-07-12T04:00:00Z", "encoder": "tmisc/2.0.0" }
}
```

Campos que **cargan peso** (no son decorativos):

- **`audio.mime`** — es el `Content-Type` que sirve el gateway. Sin él, VLC va a
  ciegas y el `<audio>` del navegador se niega a sonar. Se determina por **magic
  bytes**, nunca por la extensión del nombre del fichero.
- **`audio.ext`** — construye `/stream/{id}.{ext}`; varios clientes deducen el
  formato de la URL.
- **`audio.duration_ms`** — el catálogo y las playlists la muestran sin decodificar.
- **`ids.recording_mbid`** — el MBID de la grabación en MusicBrainz (el que escribe Picard).

## 7. Contrato HTTP del nodo

Cualquier cliente que hable HTTP con `Range` reproduce con seek, sin saber nada
de este formato. El gateway traduce cada rango a `seek(audio_offset + inicio)`.

**El cliente nunca ve el contenedor**: el `Content-Range` habla del tamaño del
*audio*, no del `.tmisc`. Para VLC, la URL es un mp3 corriente.

| Ruta | Qué devuelve |
|---|---|
| `GET/HEAD /stream/{id}[.{ext}]` | el audio. `200`, `206`, `416`. `Accept-Ranges: bytes` |
| `GET/HEAD /cover/{id}` | la imagen: el slice `[0, image_size)` |
| `GET /cover/{id}.jpg?size=300` | la portada re-encodada, ~30x más pequeña |
| `GET /track/{id}` | el JSON de metadatos + URLs absolutas |
| `GET/HEAD /file/{id}` | el `.tmisc` entero, con Range (P2P) |
| `GET /catalog?q=&artist=&album=` | listado del catálogo |
| `GET /playlist.xspf` · `/playlist.m3u8` | la cola, para abrirla en VLC |
| `GET /.well-known/tmisc.json` | descriptor del nodo |

Detalles que hay que respetar:

- **`HEAD` no lleva cuerpo.** VLC y mpv hacen HEAD para sondear si hay seek.
- **`416` lleva `Content-Range: bytes */{size}`.** Es obligatorio.
- **Rango sufijo `bytes=-N`.** Firefox lo usa para localizar el átomo `moov` de
  un m4a cuando está al final.
- **CORS con `expose_headers`.** Sin `Content-Range` expuesto, el JS del
  navegador no puede leerlo.
- **XSPF usa milisegundos y M3U segundos.** Confundirlos rompe el seek en VLC.

## 8. Protocolo P2P v1 (`p2p-v1`)

Opcional. Un nodo con P2P activo escucha en **otro puerto** (por convención
`puerto+1`) con **TLS 1.3** y un certificado X.509 autofirmado. El player local
sigue en HTTP plano; este canal es solo entre nodos.

### 8.1 Identidad

- Cada nodo tiene un par **Ed25519**. `node_id = BLAKE3-256(clave pública)`, hex.
- El certificado TLS se autofirma con ESA clave (CN = node_id) y es
  **determinista** (serial y fechas fijos): regenerable byte a byte desde la
  clave. Verificar el cert pineado de un peer ES verificar su identidad.
- Anuncio coherente obligatorio: al recibir una identidad se comprueba
  `node_id == BLAKE3(pubkey)` **y** que la clave dentro del cert es esa pubkey.

### 8.2 Pairing (SAS, estilo Bluetooth Numeric Comparison)

El receptor abre una ventana de accept de 120 s (fuera de ella, 403; un intento
la consume). Sobre TLS aún sin verificar:

```
A→B  POST /p2p/v1/pair/start   {node_id, pubkey, cert_pem, name, peer_port,
                                commit = BLAKE3(nonce_A)}      nonce_A: 32 B
B→A  200 {session_id, node_id, pubkey, cert_pem, name, nonce_B}
A→B  POST /p2p/v1/pair/reveal  {session_id, nonce_A}    (B valida el commit)

SAS = int(BLAKE3("tmisc-pair-v1" ‖ min(pkA,pkB) ‖ max(pkA,pkB)
               ‖ nonce_A ‖ nonce_B ‖ BLAKE3(cert_B))[0:4]) mod 10^6
```

Ambos humanos comparan los 6 dígitos y confirman. El iniciador cierra con
`POST /p2p/v1/pair/confirm {session_id}`; el receptor responde
`{"state":"confirmed"}` solo si su humano ya confirmó (si no, `"waiting"` y el
iniciador reintenta). Cada lado persiste entonces `{node_id, pubkey, cert_pem,
name, addr}` del otro. El commit impide moler el SAS: probabilidad 10⁻⁶ por
intento, un intento por ventana.

### 8.3 Sesión (peers ya emparejados, sobre TLS pineado)

```
A→B  POST /p2p/v1/session/challenge {node_id_A}
       -> 403 si A no está emparejado · 200 {challenge, node_id_B}
          challenge: 32 B hex, UN solo uso, TTL 60 s
A→B  POST /p2p/v1/session {node_id_A, challenge, sig}
       sig = Ed25519(sk_A, "tmisc-p2p-v1" ‖ challenge ‖ node_id_A
                     ‖ node_id_B ‖ BLAKE3(cert_pem_B))          (hex)
       -> 200 {session, expires_in}
```

La firma incluye el cert del servidor tal y como lo ve el cliente (*channel
binding*): capturada, no sirve contra otro nodo. Después:
`Authorization: Bearer <session>`; ante un 401, re-handshake (una vez).

### 8.4 Datos

| Ruta | Qué devuelve |
|---|---|
| `GET /p2p/v1/catalog?q=` | `{node_id, total, items[]}` — SOLO el catálogo local |
| `GET /p2p/v1/track/{id}` | offsets y tamaños: `audio_offset`, `audio_size`, `file_size`, `image_size`, `mime`, `ext`, `streamable`, `meta` |
| `GET/HEAD /p2p/v1/file/{id}` | el `.tmisc` entero, con `Range` |

`/p2p/v1/file` es el ÚNICO endpoint de datos. Para servir un rango `[s,e]` del
audio de un peer: pide `Range: bytes=(audio_offset+s)-(audio_offset+e)` del
file. HEAD y 416 se responden con la metadata de `/track`, sin tocar la red.

Regla anti-ciclos (normativa): los endpoints `/p2p/v1/*` responden únicamente
con el catálogo local del nodo — **nunca** consultan a otros peers. Un id que
el nodo no tiene es 404, siempre.

Al descargar un `.tmisc` entero, verifica `BLAKE3(slice del audio) == id`
antes de indexarlo; si no cuadra, descarta el fichero.

### 8.5 Descubrimiento

mDNS, servicio **`_tmisc._tcp.local.`**, TXT: `id` (node_id), `name`, `v=2`,
`proto=p2p-v1`; el puerto anunciado es el del listener TLS. Descubierto ≠
confiado: solo autoriza el pairing. Debe poder operarse sin multicast
(direcciones explícitas).

## 9. Compatibilidad

Los `.tmisc` **v1 en modo append no necesitan migración**: su offset de audio es
derivable (`size - 27 - audio_size`), así que se sirven con Range igual que un v2.
Solo les falta el hash (se calcula al indexar) y los metadatos.

Los `.tmisc` en modo **LSB** (v1 o v2) guardan el audio en los bits menos
significativos de los píxeles: **no son streameables**, hay que decodificar la
imagen entera antes de servir un solo byte. Cuestan ~10.4x el tamaño del audio.
Se conservan por compatibilidad; no son el camino de producción.
