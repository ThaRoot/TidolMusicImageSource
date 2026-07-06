# TMISC — Microservicio de esteganografía de audio en imagen

Codifica audio dentro de imágenes **BMP**. El archivo resultante lleva extensión
**`.tmisc`** pero **es un BMP válido**: se abre en cualquier visor de imágenes y,
además, contiene el audio recuperable.

> `.tim` y `.tdl` ya están ocupados por varios formatos (textura de PlayStation,
> secuencias Vixen, variantes TIFF; ToDoList, MathType, IEEE 1451.4…). `.tmisc`
> está libre, por eso es la extensión elegida.

## Dos modos

| Modo | Cómo oculta el audio | Capacidad | La imagen se ve… |
|------|----------------------|-----------|------------------|
| `lsb` | En los bits menos significativos de los píxeles (esteganografía real) | ~3 bits/píxel | casi idéntica al cover |
| `append` | Anexado tras los datos del BMP + trailer | ilimitada | intacta (visores ignoran lo de más) |
| `auto` | LSB si cabe en el cover; si no, append | — | — |

Si no envías un cover, el servicio **genera uno** a partir de los propios bytes
del audio (una "huella visual") y usa LSB.

## Especificación del formato

**Cabecera LSB** (embebida en los píxeles, big-endian):
```
magic(6)="TMISC1" | version(1) | mode(1)=1 | fmt(8 ascii) | audio_size(u64) | crc32(u32)
```
seguida de los bytes del audio, todo escrito bit a bit en los LSB del canal RGB.

**Trailer APPEND** (al final físico del archivo, big-endian):
```
magic(6)="TMISC1" | mode(1)=2 | fmt(8 ascii) | audio_size(u64) | crc32(u32)
```
el audio son los `audio_size` bytes justo antes del trailer.

**Detección**: el decodificador lee primero los últimos 27 bytes (trailer). Si el
magic coincide → APPEND. Si no → intenta leer la cabecera desde los LSB. El
`crc32` valida integridad en ambos modos.

## Uso — servidor

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

```bash
# ocultar audio (auto genera cover y usa LSB)
curl -X POST http://localhost:8000/encode \
  -F "audio=@cancion.wav" -F "mode=auto" -o salida.tmisc

# ocultar en un cover propio
curl -X POST http://localhost:8000/encode \
  -F "audio=@cancion.wav" -F "cover=@foto.png" -o salida.tmisc

# recuperar el audio
curl -X POST http://localhost:8000/decode \
  -F "file=@salida.tmisc" -o recuperado.wav
```

## Uso — CLI (sin servidor)

```bash
python cli.py encode cancion.wav -o salida.tmisc -c foto.png -m auto
python cli.py decode salida.tmisc -o recuperado.wav
python cli.py info   salida.tmisc
```

## Docker

```bash
docker build -t tmisc .
docker run -p 8000:8000 tmisc
```

## Limitaciones y notas

- **LSB no sobrevive a recompresión.** Si el `.tmisc` se re-guarda como JPG/PNG o
  se recorta, el audio en LSB se pierde. Por eso se usa BMP (sin compresión con
  pérdida). El modo `append` sí sobrevive a copiar/mover, pero no a que un editor
  re-exporte la imagen.
- **Esto es ocultación, no cifrado.** Para confidencialidad real, cifra el audio
  (p. ej. AES-GCM) *antes* de codificarlo; el formato lo transporta igual.
- El modo `append` es "ocultar a plena vista", no esteganografía estadísticamente
  indetectable. Para robustez frente a análisis, LSB con clave de dispersión
  pseudoaleatoria sería el siguiente paso.
