import sqlite3
import struct
from acd.l5x.module_references import resolve


def raw(kind, runtime=0, target=None):
    value=bytearray(82)
    struct.pack_into('<H',value,10,kind)
    struct.pack_into('<H',value,66,runtime)
    if target is not None:value.extend(struct.pack('<III',0x66,4,target))
    struct.pack_into('<II',value,74,len(value)-74,1 if target is not None else 0)
    return bytes(value)


def test_reference_is_resolved_by_target_not_display_prefix():
    db=sqlite3.connect(':memory:');cur=db.cursor()
    cur.execute('CREATE TABLE comps(object_id,parent_id,comp_name,record)')
    cur.executemany('INSERT INTO comps VALUES(?,?,?,?)',[
        (1,0,'Controller',b''),(2,1,'RxTagCollection',b''),
        (3,1,'RxMapDeviceCollection',b''),(4,2,'MisleadingName',raw(0x6b,0x1069,5)),
        (5,3,'RealModule',raw(0x69)),(6,2,'__Map:Ordinary',raw(0x6b,0xc4))])
    names={4:'MisleadingName',6:'__Map:Ordinary'}
    from acd.record.attribute_bounds import attribute_layout
    with attribute_layout('complete-counted'):
        assert resolve(cur,names)=={}
    assert names=={4:'RealModule',6:'__Map:Ordinary'}
    cur.execute('UPDATE comps SET parent_id=99 WHERE object_id=3')
    with attribute_layout('complete-counted'):
        assert 4 in resolve(cur,names) and 4 not in names
    db.close()
