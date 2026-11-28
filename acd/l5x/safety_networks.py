"""Safety network IDs in the verified module metadata revision."""
import re
import struct


def number(value):
    return '16#' + '_'.join(f'{value:016x}'[i:i+4] for i in range(0, 16, 4))


def decode(version, metadata):
    if version != 166:
        return None, {}
    if len(metadata) != 946:
        raise ValueError('Unsupported safety-network metadata length')
    value = int.from_bytes(metadata[0x131:0x137], 'little')
    count = struct.unpack_from('<H', metadata, 0x36e)[0]
    if 0x370 + 8 * count > len(metadata):
        raise ValueError('Safety-network table exceeds module metadata')
    ports = {}
    seen = set()
    for i in range(count):
        offset = 0x370 + 8 * i
        port = struct.unpack_from('<H', metadata, offset)[0]
        if port in seen:
            raise ValueError('Duplicate safety-network port identity')
        seen.add(port)
        network = int.from_bytes(metadata[offset+2:offset+8], 'little')
        if network:
            ports[port] = number(network)
    return number(value) if value else None, ports


def port_attributes(xml, networks):
    found = set()
    def replace(match):
        port = int(match[2])
        if port in found:
            raise ValueError('Duplicate physical port identity')
        found.add(port)
        return match[1] + (f' SafetyNetwork="{networks[port]}"' if port in networks else '')
    result = re.sub(r'(<Port Id="(\d+)")', replace, xml)
    if set(networks) - found:
        raise ValueError('Safety network refers to an unresolved physical port')
    return result
