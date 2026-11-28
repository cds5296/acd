"""Validate the module identity descriptor before consuming its fields."""
import struct

from acd.record.rx import LegacyRxGeneric, RxGeneric


class ModuleDescriptorError(ValueError):
    pass


def read_module_descriptor(raw):
    try:
        parsed = RxGeneric.from_bytes(raw)
    except (EOFError, ValueError, struct.error) as exc:
        raise ModuleDescriptorError(f'Unreadable module descriptor: {exc}') from exc
    if parsed.cip_type != 0x69:
        raise ModuleDescriptorError('Unexpected object class in module collection')
    attrs = parsed.extended_records
    descriptors = [attr for attr in attrs if attr.attribute_id == 1]

    # In compatibility mode the generated reader omits the final attribute.
    # Its ID can invalidate an earlier descriptor without decoding its value.
    if (descriptors and not isinstance(parsed, LegacyRxGeneric)
            and getattr(parsed, 'count_record', len(attrs)) == len(attrs) + 1):
        last = 82 + sum(8 + attr.len_value for attr in attrs)
        if last + 4 > len(raw):
            raise ModuleDescriptorError('Truncated final module attribute header')
        if struct.unpack_from('<I', raw, last)[0] == 1:
            raise ModuleDescriptorError('Duplicate module descriptor')
    if len(descriptors) > 1:
        raise ModuleDescriptorError('Duplicate module descriptor')
    if not descriptors:
        raise ModuleDescriptorError('Missing module descriptor')

    descriptor = descriptors[0]
    metadata = bytes(descriptor.value)
    minimum = {166: 946, 173: 1504}.get(parsed.record_format_version, 0x30)
    if len(metadata) < minimum or len(metadata) < getattr(descriptor, 'len_value', len(metadata)):
        raise ModuleDescriptorError('Truncated module descriptor')
    return parsed, metadata
