"""Excepciones del formato .tmisc."""


class TmiscError(Exception):
    pass


class CapacityError(TmiscError):
    """El audio no cabe en el cover (solo aplica al modo LSB legacy)."""


class NotTmiscError(TmiscError):
    """El archivo no contiene audio TMISC (ni footer v2, ni trailer v1, ni LSB)."""


class ChecksumError(TmiscError):
    """CRC32 o BLAKE3 no coinciden: el archivo está alterado o corrupto."""


class CorruptFooterError(TmiscError):
    """El footer existe pero es incoherente (crc malo, offsets fuera del archivo)."""


class UnsupportedVersionError(TmiscError):
    """Footer de una versión del formato que este lector no entiende."""
