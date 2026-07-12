"""Compatibilidad con v1: cero migración obligatoria.

Los fixtures de tests/data/ se generaron con el código v1 ORIGINAL (el que estaba
en tmisc.py antes de este rediseño; sigue en el historial de git). Si un cambio
futuro rompe la lectura de estos ficheros, estos tests fallan.
"""

from tmisc.container import probe, read_audio
from tmisc.decode import mode_name


def test_v1_append_se_lee_sin_migrar(v1_append):
    info = probe(v1_append)
    assert info.version == 1
    assert mode_name(info) == "append-v1"
    # Lo importante: el offset del audio es derivable, así que se sirve con Range
    # exactamente igual que un v2.
    assert info.streamable
    assert info.audio_offset > 0
    assert info.audio_size > 0


def test_v1_append_es_streameable_por_slices(v1_append):
    """El gateway sirve un v1 sin tocarlo: solo necesita el offset."""
    info = probe(v1_append)
    with open(v1_append, "rb") as fh:
        fh.seek(info.audio_offset)
        crudo = fh.read(info.audio_size)
    assert crudo == read_audio(info)
    assert crudo[:3] == b"ID3" or crudo[0] == 0xFF     # es un mp3 de verdad


def test_v1_append_mime_correcto(v1_append):
    """El mime se re-deriva de los magic bytes, no del campo `fmt` del trailer."""
    assert probe(v1_append).mime == "audio/mpeg"


def test_v1_lsb_se_detecta_pero_no_es_streameable(v1_lsb):
    info = probe(v1_lsb)
    assert info.version == 1
    assert mode_name(info) == "lsb"
    assert not info.streamable          # hay que decodificar los píxeles primero
    assert len(read_audio(info)) == info.audio_size


def test_v1_lsb_confirma_el_sobrecoste_de_10x(v1_lsb):
    """El motivo por el que LSB no es el camino de producción."""
    info = probe(v1_lsb)
    assert info.file_size / info.audio_size > 9


def test_v1_indexado_junto_a_v2(library):
    """Un v1 y dos v2 conviven; el v1 se sirve 'direct', sin migrarlo.

    Y de paso el dedupe queda demostrado: el fixture v1 lleva DENTRO el mismo
    tone.mp3 que uno de los v2, así que aunque son 3 ficheros distintos en disco
    (con portadas distintas y hasta formatos de contenedor distintos), el
    catálogo ve 2 tracks lógicos. El id es el hash del AUDIO.
    """
    _lib, cat = library
    total, tracks = cat.search(limit=10)

    assert total == 2                                     # tracks lógicos
    assert cat.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3   # ficheros
    assert {t.streamable for t in tracks} == {"direct"}


def test_migrate_conserva_el_audio_byte_a_byte(v1_append, tmp_path):
    """v1 -> v2 no recomprime nada: el audio es el mismo."""
    import io

    from tmisc.encode import encode_to_file

    original = read_audio(probe(v1_append))
    out = str(tmp_path / "migrado.tmisc")
    encode_to_file(io.BytesIO(original), len(original), out)

    nuevo = probe(out)
    assert nuevo.version == 2
    assert read_audio(nuevo) == original
