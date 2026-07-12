"""El footer: tamaños, round-trip y rechazo de corrupción."""

import os
import struct

import pytest

from tmisc import format as fmt
from tmisc.errors import CorruptFooterError, UnsupportedVersionError


def test_tamanos_normativos():
    """Si estos números cambian, cambia el formato: los lectores de terceros rompen."""
    assert fmt.FOOTER_BODY_SIZE == 112
    assert fmt.FOOTER_TAIL_SIZE == 8
    assert fmt.FOOTER_SIZE == 120
    assert fmt.TRAILER_V1_SIZE == 27


def _footer(**kw) -> fmt.Footer:
    base = dict(
        file_size=1_000_000, image_size=250_000, audio_offset=250_000,
        audio_size=700_000, meta_offset=950_000, meta_size=400,
        audio_crc32=0xDEADBEEF, audio_blake3=os.urandom(32), meta_crc32=7,
    )
    base.update(kw)
    return fmt.Footer(**base)


def test_round_trip():
    f = _footer()
    blob = f.pack()
    assert len(blob) == 120
    g = fmt.Footer.unpack(blob[:112])
    assert g.audio_offset == f.audio_offset
    assert g.audio_size == f.audio_size
    assert g.audio_blake3 == f.audio_blake3
    assert g.track_id == f.audio_blake3.hex()


def test_tail_autolocalizable():
    """Los últimos 8 bytes dicen el magic y dónde empieza el footer."""
    blob = _footer().pack()
    magic, size = struct.unpack(fmt.FOOTER_TAIL_FMT, blob[-8:])
    assert magic == b"TMISC2"
    assert size == 120


def test_footer_corrupto_se_rechaza_antes_de_creer_los_offsets():
    """Un footer manipulado no puede hacer que /stream apunte a cualquier sitio."""
    blob = bytearray(_footer().pack()[:112])
    blob[24] ^= 0xFF                       # tocar audio_offset
    with pytest.raises(CorruptFooterError):
        fmt.Footer.unpack(bytes(blob))


def test_version_futura_se_rechaza():
    f = _footer()
    f.version_major = 3
    with pytest.raises(UnsupportedVersionError):
        fmt.Footer.unpack(f.pack()[:112])


def test_truncamiento_detectado_en_O1():
    """file_size en el footer: si no cuadra con el fichero, está truncado o copiándose."""
    f = _footer(file_size=1_000_000)
    with pytest.raises(CorruptFooterError, match="truncado|declara"):
        f.validate_against(999_999)


@pytest.mark.parametrize("kw,motivo", [
    (dict(audio_offset=5), "el audio empezaría dentro de la cabecera BMP"),
    (dict(audio_size=10_000_000), "el audio se saldría del archivo"),
    (dict(meta_offset=100), "los metadatos solaparían el audio"),
    (dict(image_size=900_000), "la imagen solaparía el audio"),
])
def test_offsets_incoherentes(kw, motivo):
    f = _footer(**kw)
    with pytest.raises(CorruptFooterError):
        f.validate_against(f.file_size)


def test_bfsize_dice_donde_acaba_la_imagen():
    """bfSize (little-endian, a diferencia del footer) da image_size sin parsear el DIB."""
    head = b"BM" + struct.pack("<I", 750054) + b"\x00" * 8
    assert fmt.bmp_declared_size(head) == 750054
    assert fmt.bmp_declared_size(b"NOPE" + b"\x00" * 10) is None
