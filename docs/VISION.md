# Visión: tu biblioteca, tus dispositivos, tus amigos

> Documento de dirección. Fija **qué problema resolvemos** y **las decisiones ya
> tomadas**, para que cada cambio de código se pueda contrastar contra esto.
> Última revisión: 2026-11-24 (etapa 1 terminada).

## El problema

**Mi biblioteca vive en mi PC. Quiero escucharla desde mis dispositivos, dentro y
fuera de casa, y compartirla con amigos de confianza, sin servidores de terceros
y sin copiar mi colección a ningún lado.**

Lo que ya existe no lo cubre del todo:

- Navidrome / Jellyfin sirven *tu* biblioteca a *tus* dispositivos, pero no unen
  la biblioteca de un amigo con la tuya.
- Syncthing comparte carpetas, pero copia todo a todos.
- Los servicios de streaming no tienen tu música ni respetan tu colección.

Lo distintivo de TMISC es el **P2P entre nodos de confianza con pasarela**: tu
reproductor habla solo con tu nodo, y tu nodo trae de forma transparente lo que
vive en la máquina de un amigo.

## Modelo

```
     tus dispositivos                         dispositivos de tu amigo
  (iPhone, laptop, VLC…)                     (sus apps Subsonic, VLC…)
            │ HTTP / Subsonic + token                     │
            ▼ por dispositivo                             ▼
   ┌──────────────────┐   TLS fijado + Ed25519   ┌──────────────────┐
   │   TU NODO        │◄────────────────────────►│  NODO DEL AMIGO  │
   │ biblioteca local │   emparejados con SAS    │ biblioteca local │
   └──────────────────┘     (código 6 dígitos)   └──────────────────┘
            LAN en casa · Tailscale fuera de casa / entre casas
```

| Concepto | Qué es | Cómo se autentica |
| :--- | :--- | :--- |
| **Nodo** | Un TMISC en una PC con biblioteca. Uno por persona. | Par Ed25519 propio. |
| **Dispositivo** | Cliente de *tu* nodo (iPhone, laptop). **No es un nodo.** | Token propio, revocable uno a uno. |
| **Amigo** | Otro nodo, emparejado. | Pairing SAS + TLS con cert fijado (ya existe). |

## Decisiones tomadas

1. **Biblioteca indexada en sitio.** El nodo sirve tus MP3/FLAC/Opus/M4A tal
   cual están en disco: no se convierten ni se duplican, y Picard/beets siguen
   funcionando. `.tmisc` se mantiene como formato **opcional** (se puede leer y
   exportar un archivo suelto con portada), no como formato de biblioteca.
2. **El id es el hash del audio sin etiquetas.** BLAKE3 de los bytes de audio
   excluyendo ID3v2/ID3v1/APE y los bloques de metadatos de FLAC. Editar etiquetas
   no cambia el id; la deduplicación y la verificación P2P siguen funcionando.
3. **Los dispositivos usan la API Subsonic/OpenSubsonic.** No hacemos app:
   Amperfy, play:Sub, Substreamer (iOS) o Symfonium (Android) ya dan biblioteca,
   portadas, búsqueda y caché offline. Las canciones de los amigos aparecen como
   otra carpeta de música.
4. **Permisos por amigo, elegidos al emparejar.** Por defecto **solo streaming**;
   opcionalmente **streaming + caché** (la pasarela guarda copia verificada).
5. **Fuera de casa: Tailscale.** TMISC no resuelve NAT ni se expone a Internet.
   Entre casas, el amigo entra a tu tailnet o compartes solo el nodo
   (*node sharing*). mDNS no cruza Tailscale: ahí se usan direcciones explícitas.

## No-objetivos

- **No es DRM.** Los permisos son un acuerdo entre amigos de confianza: quien
  puede escuchar un stream puede grabarlo. El permiso "solo streaming" evita copias
  *accidentales* y descontroladas, no a un amigo malicioso (para eso: no lo emparejes).
- **No se expone a Internet** ni se abren puertos en el router.
- **No se integra con Tidol.** Comparte origen y nombre, nada más: TMISC es un
  proyecto independiente. Una integración futura habría que diseñarla aparte.
- **Sin transcodificación** en la primera etapa (ver preguntas abiertas).

## Plan por etapas

Cada etapa se puede usar sola y deja las pruebas en verde.

| # | Etapa | Resultado usable |
| :- | :--- | :--- |
| 1 | ✅ **Biblioteca en sitio + id sin etiquetas** | `tmisc serve --library ~/Music` sirve tu colección sin convertir nada. |
| 2 | **Tokens por dispositivo** | `tmisc devices add iphone` / `ls` / `rm`. Reemplaza el token único. |
| 3 | **API Subsonic (subconjunto)** | Amperfy en el iPhone navega y reproduce tu biblioteca. |
| 4 | **Catálogo de amigos en Subsonic** | Las canciones del amigo aparecen en la app, como otra carpeta. |
| 5 | **Permisos por amigo** | Al emparejar: "solo streaming" o "streaming + caché". |
| 6 | **Remoto por Tailscale** | Guía + escucha en la interfaz de Tailscale; probado entre dos casas. |

## Preguntas abiertas

- ~~Migración de ids~~ **Resuelto en la etapa 1:** el id se recalcula al
  indexar (esquema `b3-untagged-1`); el hash del footer de un `.tmisc` queda solo
  como integridad. El esquema se anuncia en `/.well-known/tmisc.json` y en el ping
  P2P: dos nodos con esquemas distintos no comparten ids, hay que actualizar ambos.
- ~~Opus/Ogg y M4A~~ **Resuelto:** se hashean los paquetes de audio (Ogg) y los
  átomos `mdat` (MP4). Solo los formatos desconocidos caen a "id débil".
- **Integridad P2P (descubierto en la etapa 1).** Como el id ya no cubre las
  etiquetas, la pasarela verifica además `file_b3`, el BLAKE3 del fichero entero.
- **Transcodificación para datos móviles** (FLAC → Opus 128k con ffmpeg). Subsonic
  ya tiene el parámetro `maxBitRate`; se puede añadir tras la etapa 3.
- **Subsonic con contraseña o con API key.** El esquema clásico (`t` + `s`, MD5
  de contraseña + sal) obliga a guardar la contraseña en claro; OpenSubsonic
  `apiKey` lo evita, pero no todas las apps lo soportan aún. Aceptar ambos, con el
  token del dispositivo como contraseña.
