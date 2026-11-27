"""Execution classes for the verified modern task/program metadata layouts."""
import struct


def source_class(version, metadata, kind):
    layouts = {'task': (18, 4372, 4316, '<H'), 'program': (19, 4194, 139, '<B')}
    expected_version, size, offset, fmt = layouts[kind]
    if version != expected_version or len(metadata) != size:
        return None
    word = struct.unpack_from(fmt, metadata, offset)[0]
    if word not in (0, 6):
        raise ValueError('Unsupported execution class in known metadata layout')
    return {0:'Standard', 6:'Safety'}[word]
