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


def module(tag, revision=166):
    value = bytearray(raw(0x69))
    struct.pack_into('<H', value, 8, revision)
    value.extend(struct.pack('<III', 0x140, 4, tag))
    value.extend(struct.pack('<II', 2, 0))
    struct.pack_into('<II', value, 74, len(value) - 74, 2)
    return bytes(value)


def test_reverse_reference_is_typed_live_scoped_and_unambiguous():
    db = sqlite3.connect(':memory:')
    with db:
        cur = db.cursor()
        cur.execute('CREATE TABLE comps(object_id,parent_id,comp_name,record,record_type)')
        cur.executemany('INSERT INTO comps VALUES(?,?,?,?,?)', [
            (2,1,'RxTagCollection',b'',256), (3,1,'RxMapDeviceCollection',b'',256),
            (9,8,'RxMapDeviceCollection',b'',256), (4,2,'internal',raw(0x6b,0x1069),256),
            (5,3,'Selected',module(4),256), (6,3,'Deleted',module(4),512),
            (7,9,'OtherOwner',module(4),256), (10,3,'OtherRevision',module(4,165),256)])
        names = {4:'internal'}
        assert resolve(cur, names) == {} and names[4] == 'Selected'
        cur.execute('UPDATE comps SET record_type=256 WHERE object_id=6')
        assert 4 in resolve(cur,names) and 4 not in names
        cur.execute('DELETE FROM comps WHERE object_id=6')
        cur.execute('UPDATE comps SET record=? WHERE object_id=4',(raw(0x6b,0x1069,10),))
        from acd.record.attribute_bounds import attribute_layout
        with attribute_layout('complete-counted'):
            assert 4 in resolve(cur,names) and 4 not in names
    db.close()


def test_reciprocal_identity_cannot_override_duplicate_forward_links():
    from acd.record.attribute_bounds import attribute_layout

    # Two same-controller modules. Only one has a back-reference to this tag.
    tag = bytearray(raw(0x6b, 0x1069, 5))
    tag.extend(struct.pack('<III', 0x66, 4, 6))
    struct.pack_into('<II', tag, 74, len(tag) - 74, 2)
    with sqlite3.connect(':memory:') as db:
        cur = db.cursor()
        cur.execute('CREATE TABLE comps(object_id,parent_id,comp_name,record)')
        cur.executemany('INSERT INTO comps VALUES(?,?,?,?)', [
            (2, 1, 'RxTagCollection', b''), (3, 1, 'RxMapDeviceCollection', b''),
            (4, 2, 'Display', tag), (5, 3, 'First', module(4)), (6, 3, 'Second', raw(0x69)),
        ])
        for mode in ('compat', 'complete-counted'):
            names = {4: 'Display'}
            with attribute_layout(mode):
                issues = resolve(cur, names)
            assert 4 not in names and '0x66' in issues[4]
