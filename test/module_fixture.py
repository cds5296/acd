"""Append a synthetic module record to the bundled public AOI project."""
import gzip
import struct
from pathlib import Path

from acd.l5x.export_l5x import ExportL5x
from acd.record.rx import RxGeneric
from acd.zip.write_acd import build_acd_bytes


def module_descriptor(slot=0, mask=0, network=0):
    data = bytearray(946)
    struct.pack_into('<HHH', data, 2, 1, 14, 93)
    struct.pack_into('<H', data, 10, mask)
    struct.pack_into('<HI', data, 26, 1, slot)
    data[0x131:0x137] = network.to_bytes(6, 'little')
    return data


def module_acd(directory, descriptors, *, final_padding=True):
    source = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    export = ExportL5x(source, str(directory / 'source-db'))
    try:
        collection, root = export._cur.execute(
            "SELECT c.object_id, m.record FROM comps c JOIN comps m "
            "ON m.parent_id=c.object_id WHERE c.comp_name='RxMapDeviceCollection' "
            "AND m.comp_name='Local'"
        ).fetchone()
        parent = RxGeneric.from_bytes(root).comment_id
        files = dict(export._raw_files)
        order, footer = export._file_order, export._footer_unknown
    finally:
        export._db.close()

    rx = bytearray(82)
    struct.pack_into('<HHH', rx, 8, 166, 0x69, 0xcafe)
    for descriptor in descriptors:
        value = bytearray(descriptor)
        if len(value) >= 26:
            struct.pack_into('<I', value, 22, parent)
        rx.extend(struct.pack('<II', 1, len(value)) + value)
    if final_padding:
        rx.extend(struct.pack('<II8x', 2, 8))
    struct.pack_into('<II', rx, 74, len(rx) - 74, len(descriptors) + int(final_padding))

    body = bytearray(148) + rx
    struct.pack_into('<I', body, 0, len(body))
    struct.pack_into('<HHIII', body, 8, 0, 256, 0, 0xc0debeef, collection)
    name = 'TestModule\0'.encode('utf-16-le')
    body[24:24+len(name)] = name
    compressed = files['Comps.Dat'].startswith(b'\x1f\x8b')
    dat = gzip.decompress(files['Comps.Dat']) if compressed else files['Comps.Dat']
    dat = bytearray(dat[:struct.unpack_from('<I', dat, 8)[0] + 1])
    dat.extend(struct.pack('<HI', 0xfafa, 6 + len(body)) + body)
    struct.pack_into('<I', dat, 8, len(dat) - 1)
    files['Comps.Dat'] = gzip.compress(dat) if compressed else bytes(dat)
    path = directory / 'module.ACD'
    path.write_bytes(build_acd_bytes(files, order, footer))
    return path
