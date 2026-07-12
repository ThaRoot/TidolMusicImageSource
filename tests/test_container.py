"""El contenedor: sigue siendo un BMP, el round-trip es exacto, y los offsets bastan."""

import io
import subprocess

import pytest
from PIL import Image

from tmisc.container import iter_audio_slice, probe, read_audio, read_cover_bytes, read_meta
from tmisc.decode import verify
from tmisc.encode import encode_to_file
from tmisc.errors import ChecksumError
from tmisc.hashing import hash_bytes


# --- el requisito nº1 del formato -------------------------------------------
def test_sigue_siendo_un_bmp_valido(tmisc_v2, cover_bytes):
    """Un .tmisc DEBE abrirse en cualquier visor de imágenes."""
    img = Image.open(tmisc_v2)
    img.load()                                    # decodifica los píxeles de verdad
    assert img.format == "BMP"

    original = Image.open(io.BytesIO(cover_bytes)).convert("RGB").resize(img.size)
    assert img.size == original.size


def test_file_lo_reconoce_como_bitmap(tmisc_v2):
    out = subprocess.run(["file", "-b", tmisc_v2], capture_output=True, text=True).stdout
    assert "bitmap" in out.lower()


# --- integridad --------------------------------------------------------------
def test_round_trip_byte_exacto(tmisc_v2, mp3_bytes):
    info = probe(tmisc_v2)
    assert read_audio(info) == mp3_bytes


def test_el_id_es_el_hash_del_audio_no_del_archivo(tmisc_v2, mp3_bytes):
    info = probe(tmisc_v2)
    assert info.audio_blake3 == hash_bytes(mp3_bytes)


def test_dos_portadas_distintas_mismo_id(tmp_path, mp3_bytes, cover_bytes):
    """Content-addressing: la misma canción con otra portada es EL MISMO track."""
    from PIL import Image
    otra = io.BytesIO()
    Image.new("RGB", (200, 200), (200, 30, 30)).save(otra, format="PNG")

    a, b = str(tmp_path / "a.tmisc"), str(tmp_path / "b.tmisc")
    encode_to_file(io.BytesIO(mp3_bytes), len(mp3_bytes), a, cover_data=cover_bytes)
    encode_to_file(io.BytesIO(mp3_bytes), len(mp3_bytes), b, cover_data=otra.getvalue())

    assert probe(a).track_id == probe(b).track_id      # dedupe automático
    # Y aun así son ficheros distintos: la portada cambia. (El tamaño no, porque
    # un BMP de 500x500x24 siempre ocupa lo mismo; hay que mirar los bytes.)
    assert read_cover_bytes(probe(a)) != read_cover_bytes(probe(b))


def test_verify_detecta_manipulacion(tmisc_v2):
    info = probe(tmisc_v2)
    verify(info)                                       # intacto: pasa

    with open(tmisc_v2, "r+b") as fh:                  # tocar un byte del audio
        fh.seek(info.audio_offset + 500)
        fh.write(b"\xff")
    with pytest.raises(ChecksumError):
        verify(probe(tmisc_v2))


# --- lo que hace posible el seek ---------------------------------------------
def test_los_offsets_bastan_sin_la_libreria(tmisc_v2, mp3_bytes):
    """La prueba de que la rocola puede leerlo en el stack que sea: dos enteros."""
    info = probe(tmisc_v2)
    with open(tmisc_v2, "rb") as fh:
        fh.seek(info.audio_offset)
        crudo = fh.read(info.audio_size)
    assert crudo == mp3_bytes


def test_slice_arbitrario(tmisc_v2, mp3_bytes):
    info = probe(tmisc_v2)
    trozo = b"".join(iter_audio_slice(info, start=1000, length=500))
    assert trozo == mp3_bytes[1000:1500]


def test_slice_no_se_sale_del_audio(tmisc_v2, mp3_bytes):
    """Pedir más allá del final devuelve solo lo que hay, nunca bytes del footer."""
    info = probe(tmisc_v2)
    trozo = b"".join(iter_audio_slice(info, start=len(mp3_bytes) - 10, length=99999))
    assert trozo == mp3_bytes[-10:]


def test_mime_por_magic_bytes_no_por_nombre(tmp_path, flac_bytes):
    """El Content-Type sale del contenido; el nombre del fichero miente."""
    out = str(tmp_path / "mentira.mp3.tmisc")
    encode_to_file(io.BytesIO(flac_bytes), len(flac_bytes), out,
                   source_filename="cancion.mp3")      # nombre engañoso a propósito
    info = probe(out)
    assert info.mime == "audio/flac"
    assert info.ext == "flac"


def test_portada_extraible_sin_pillow(tmisc_v2):
    info = probe(tmisc_v2)
    data = read_cover_bytes(info)
    assert data[:2] == b"BM"
    assert len(data) == info.image_size


def test_metadatos(tmisc_v2):
    meta = read_meta(probe(tmisc_v2))
    assert meta.track.title == "Paranoid Android"
    assert meta.track.artist == "Radiohead"
    assert meta.audio.mime == "audio/mpeg"


# --- el bug de v1 que se arregló ---------------------------------------------
def test_la_portada_autogenerada_es_de_tamano_fijo(tmp_path, mp3_bytes):
    """EL BUG DE v1: el cover se dimensionaba por capacidad LSB incluso en append,
    lo que daba 11.4x. Ahora es fijo, así que el overhead tiende a 1.0x."""
    corto, largo = mp3_bytes[:20_000], mp3_bytes * 8

    a, b = str(tmp_path / "corto.tmisc"), str(tmp_path / "largo.tmisc")
    fa = encode_to_file(io.BytesIO(corto), len(corto), a)
    fb = encode_to_file(io.BytesIO(largo), len(largo), b)

    assert fa.image_size == fb.image_size              # la portada NO crece con el audio
    assert fb.file_size / len(largo) < 1.8             # y el overhead se diluye
