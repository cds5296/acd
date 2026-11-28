"""Verified module electronic-key masks; unknown masks stay unresolved."""
import struct


def electronic_keying(attribute):
    if len(attribute) < 12:
        raise ValueError('Truncated module electronic-key mask')
    mask = struct.unpack_from('<H', attribute, 10)[0]
    if mask not in (0, 0x1f):
        raise ValueError('Unsupported module electronic-key mask')
    return {0: 'Disabled', 0x1f: 'CompatibleModule'}[mask]
