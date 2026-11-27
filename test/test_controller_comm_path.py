from pathlib import Path
import struct

import pytest

from acd.l5x.elements import ControllerBuilder
from acd.l5x.export_l5x import ExportL5x
from acd.record.rx import RxGeneric


def record(header, attributes):
    raw = bytearray(header[:82])
    for key, value in attributes:
        raw.extend(struct.pack('<II', key, len(value)) + value)
    # Compatibility layout with a final unused attribute.
    raw.extend(struct.pack('<II', 0xff, 4))
    struct.pack_into('<II', raw, 74, len(raw) - 74, len(attributes) + 1)
    return raw


DIRECT = r'Driver\192.0.2.10'
BACKPLANE = DIRECT + '\\Backplane\\'


@pytest.mark.parametrize('route,slot,expected', [
    (DIRECT, 0, DIRECT), (DIRECT, None, DIRECT), (DIRECT, 0xffffffff, DIRECT),
    (BACKPLANE, 4, BACKPLANE + '4'), (BACKPLANE, 0, BACKPLANE + '0'),
    (BACKPLANE, 0xffffffff, None), (BACKPLANE, None, None), (BACKPLANE, 'short', None),
    (BACKPLANE + '12', 3, BACKPLANE + '12'),
])
def test_controller_assembles_only_a_known_backplane_slot(tmp_path, route, slot, expected):
    acd = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    export = ExportL5x(acd, str(tmp_path / 'project'))
    try:
        cur = export._cur
        oid, root = cur.execute('SELECT object_id,record FROM comps WHERE parent_id=0 AND record_type=256').fetchone()
        attrs = [(a.attribute_id, bytes(a.value)) for a in RxGeneric.from_bytes(root).extended_records
                 if a.attribute_id != 0x6a]
        attrs.append((0x6a, (route + '\0').encode('utf-16-le')))
        cur.execute('UPDATE comps SET record=? WHERE object_id=?', (record(root, attrs), oid))
        collection, = cur.execute("SELECT object_id FROM comps WHERE parent_id=? AND comp_name='RxMapDeviceCollection'",
                                  (oid,)).fetchone()
        cur.execute('DELETE FROM comps WHERE parent_id=?', (collection,))
        if slot is not None:
            header = bytearray(82)
            struct.pack_into('<H', header, 10, 0x69)
            payload = bytearray(0x30) if slot != 'short' else b''
            if payload:
                struct.pack_into('<I', payload, 0x1c, slot)
            cur.execute("INSERT INTO comps(object_id,parent_id,comp_name,seq_number,record_type,record) "
                        "VALUES (?,?,?,?,?,?)", (1234567, collection, 'Local', 0, 256,
                                                record(header, [(1, payload)])))
        assert ControllerBuilder(cur).build().comm_path == expected
    finally:
        export._db.close()
