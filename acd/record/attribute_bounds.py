"""Validate the complete counted-attribute contract for supported Rx records.

These bounds checks do not identify a layout. Inflated final-attribute lengths
and outer trailers can mimic complete counted attributes. Recovery requires an
explicit caller assertion of the complete-counted contract; compat is default.
Opaque FAFA records are deliberately outside this contract.
"""
import struct
from contextlib import contextmanager
from contextvars import ContextVar

_LAYOUT = ContextVar('acd_rx_attribute_layout', default='compat')


def counted_layout_enabled():
    return _LAYOUT.get() == 'complete-counted'


@contextmanager
def attribute_layout(name=None):
    """Caller assertion, not autodetection: outer trailers can mimic values."""
    if name is None:
        yield
        return
    if name not in ('compat', 'complete-counted'):
        raise ValueError('Unknown Rx attribute layout')
    token = _LAYOUT.set(name)
    try:
        yield
    finally:
        _LAYOUT.reset(token)


COUNTED_TYPES = {0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x338}


class DuplicateModuleDescriptor(ValueError):
    """Retain the validated extent for a scoped module diagnostic."""

    def __init__(self, bounded_end):
        super().__init__('Duplicate module descriptor')
        self.bounded_end = bounded_end


class AmbiguousModuleLink(ValueError):
    """A typed module reference contains competing forward links."""

    def __init__(self, bounded_end=None):
        super().__init__("Duplicate 0x66 module-reference attributes")
        self.bounded_end = bounded_end


def complete_attributes(raw):
    if len(raw) < 82 or struct.unpack_from('<H', raw, 10)[0] not in COUNTED_TYPES:
        return None
    declared, count = struct.unpack_from('<II', raw, 74)
    if count > (len(raw) - 82) // 8:
        return None
    position, attrs = 82, []
    for _ in range(count):
        if position + 8 > len(raw):
            return None
        key, size = struct.unpack_from('<II', raw, position)
        end = position + 8 + size
        if end > len(raw):
            return None
        attrs.append((key, bytes(raw[position + 8:end])))
        position = end
    if position not in (declared + 74, declared + 78):
        return None
    if len(raw) - position not in (0, 4):
        return None
    if len({key for key, value in attrs}) != len(attrs):
        # The modern RxTag header and the entire counted extent are now verified.
        # Keep the transport boundary so Comps can retain this invalid source
        # object for a location-specific diagnostic without choosing a link.
        if (struct.unpack_from("<H", raw, 10)[0] == 0x6B
                and struct.unpack_from("<H", raw, 66)[0] == 0x1069
                and sum(key == 0x66 for key, _ in attrs) > 1):
            raise AmbiguousModuleLink(position)
        if struct.unpack_from('<H', raw, 10)[0] == 0x69 and sum(key == 1 for key, _ in attrs) > 1:
            raise DuplicateModuleDescriptor(position)
        raise ValueError('Duplicate counted Rx attribute identity')
    return position, attrs
