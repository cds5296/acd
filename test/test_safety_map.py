import sqlite3
import struct
import pytest
from acd.l5x.safety_map import read_pairs


def frame(kind, payload):
    record = bytearray(24) + payload
    struct.pack_into('<I', record, 0, len(record) - 4)
    struct.pack_into('<I', record, 16, kind)
    return bytes(record)


def index(ids):
    return frame(0x899, struct.pack('<H', len(ids)) + b''.join(struct.pack('<I', oid) for oid in ids) + bytes(2))


def pair(left, right):
    return frame(0x89b, b''.join(b'\xff\xfe\xff\x0a' + f'@{oid:08x}@'.encode('utf-16-le')
                                  for oid in (left, right)))


def test_renamed_task_and_no_signature_still_resolve_map():
    db=sqlite3.connect(':memory:');cur=db.cursor()
    cur.executescript('CREATE TABLE comps(object_id,parent_id,comp_name,record_type);'
                      'CREATE TABLE nameless(object_id,parent_id,record);')
    cur.executemany('INSERT INTO comps VALUES(?,?,?,?)',[
        (1,0,'Controller',256),(2,1,'RxTaskCollection',256),
        (3,2,'AnyRenamedTask',256),(4,1,'RxTagCollection',256),
        (5,4,'CompletelyDifferent',256),(6,4,'SafetyTarget',257)])
    cur.executemany('INSERT INTO nameless VALUES(?,?,?)',[
        (10,3,index([11])),(11,10,pair(5,6))])
    assert read_pairs(cur,1)==([('CompletelyDifferent','SafetyTarget')],[])
    cur.execute('UPDATE comps SET record_type=512 WHERE object_id=5')
    pairs,issues=read_pairs(cur,1)
    assert pairs==[] and len(issues)==1
    cur.execute('UPDATE comps SET record_type=256 WHERE object_id=5')
    cur.execute('DELETE FROM comps WHERE object_id=6')
    pairs,issues=read_pairs(cur,1)
    assert pairs==[] and len(issues)==1
    db.close()

def test_index_order_overrides_record_insertion_order():
    from acd.l5x.safety_map import indexed_pairs
    assert indexed_pairs(index([7, 4]), [(4,b'a'), (7,b'b')]) == [(7,b'b'), (4,b'a')]


@pytest.mark.parametrize('ids,rows', [([1,1], [(1,b'a')]), ([1], []),
                                      ([], [(1,b'a')]), ([1], [(1,b'a'), (1,b'b')])])
def test_unverified_or_ambiguous_pair_index_is_reported(ids,rows):
    from acd.l5x.safety_map import indexed_pairs
    with pytest.raises(ValueError):
        indexed_pairs(index(ids), rows)
