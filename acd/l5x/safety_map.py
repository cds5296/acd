"""Source OID-based safety mappings, independent of task names/signatures."""
import re
import struct
from xml.sax.saxutils import escape

REFERENCE = re.compile(rb'@\x00(?:[0-9a-fA-F]\x00){8}@\x00')


def indexed_pairs(index, rows):
    """Return the child records in the container's counted object-ID order."""
    if len(index) < 28 or struct.unpack_from('<I', index)[0] != len(index) - 4:
        raise ValueError('invalid index extent')
    count = struct.unpack_from('<H', index, 24)[0]
    if len(index) != 28 + count * 4 or index[-2:] != bytes(2):
        raise ValueError('unsupported index layout')
    ids = [item[0] for item in struct.iter_unpack('<I', index[26:-2])]
    records = dict(rows)
    if len(records) != len(rows) or len(set(ids)) != count or set(ids) != records.keys():
        raise ValueError('duplicate, orphan or missing mapping identity')
    return [(oid, records[oid]) for oid in ids]


def read_pairs(cur, controller_id):
    pairs, issues = [], []
    tasks = cur.execute('SELECT object_id FROM comps WHERE record_type != 512 AND parent_id IN '
        "(SELECT object_id FROM comps WHERE parent_id=? AND comp_name='RxTaskCollection')",
        (controller_id,)).fetchall()
    for (task,) in tasks:
        containers = cur.execute('SELECT object_id, record FROM nameless WHERE parent_id=?', (task,)).fetchall()
        for container, index in containers:
            if index[16:20] != b'\x99\x08\x00\x00':
                continue
            rows = cur.execute('SELECT object_id,record FROM nameless WHERE parent_id=?', (container,)).fetchall()
            try:
                ordered = indexed_pairs(bytes(index), rows)
            except ValueError as exc:
                issues.append(f'Invalid safety map container {container}: {exc}')
                continue
            for oid, raw in ordered:
                if (len(raw) != 72 or raw[16:20] != b'\x9b\x08\x00\x00'
                        or struct.unpack_from('<I', raw)[0] != len(raw) - 4
                        or any(raw[n:n+4] != b'\xff\xfe\xff\x0a' for n in (24, 48))
                        or not all(REFERENCE.fullmatch(raw[n:n+20]) for n in (28, 52))):
                    issues.append(f'Invalid safety mapping record {oid}')
                    continue
                names = []
                for kind, start in enumerate((28, 52)):
                    ref = int(raw[start+2:start+18:2], 16)
                    result = cur.execute('SELECT t.comp_name,t.record_type FROM comps t '
                        'JOIN comps c ON c.object_id=t.parent_id WHERE t.object_id=? '
                        "AND t.record_type != 512 AND c.record_type != 512 "
                        "AND c.parent_id=? AND c.comp_name='RxTagCollection'", (ref,controller_id)).fetchall()
                    if len(result) != 1 or (result[0][1] & 1) != kind:
                        issues.append(f'Unresolved or incorrectly classified safety mapping record {oid}')
                        break
                    names.append(result[0][0])
                if len(names) == 2:
                    pairs.append(tuple(names))
    return pairs, issues


def materialize(controller, cur):
    roots = cur.execute('SELECT object_id FROM comps WHERE parent_id=0 AND record_type=256').fetchall()
    if len(roots) != 1:
        raise ValueError('Ambiguous controller for safety map')
    pairs, issues = read_pairs(cur, roots[0][0])
    controller._safety_map_pairs = pairs
    controller._safety_map_issues = issues
    if pairs:
        text = ', '.join(escape(a+'='+b) for a,b in pairs)
        controller._safety_map_xml = '<SafetyInfo><SafetyTagMap>'+text+'</SafetyTagMap></SafetyInfo>'
