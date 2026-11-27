from types import SimpleNamespace
import xml.etree.ElementTree as ET
from acd.l5x.annotations import comments_xml
import sqlite3
import struct
from acd.l5x import annotations


def test_mixed_resolved_unresolved_and_array_operands():
    tag = SimpleNamespace(_comments=[('.Zed','z'),('.!00000001[2]','two'),
        ('.!00000001[1]','one'),('.Alpha','a')], _hex_member_map={1:('Data',3)},
        _sanitize_xml_text=lambda text: text)
    xml = ET.fromstring(comments_xml(tag))
    assert [e.get('Operand') for e in xml] == ['.Data[1]','.Data[2]','.Alpha','.Zed']


def test_distinct_identical_text_and_cdata_end_are_preserved():
    tag = SimpleNamespace(_comments=[('2','same ]]>'),('1','same ]]>')],
        _sanitize_xml_text=lambda text: text)
    xml = ET.fromstring(comments_xml(tag))
    assert [e.get('Operand') for e in xml] == ['.1','.2']
    assert [e.text for e in xml] == ['same ]]>','same ]]>']


def test_member_definition_identity_map(monkeypatch):
    db = sqlite3.connect(':memory:')
    cur = db.cursor()
    cur.execute('CREATE TABLE comps(object_id,parent_id,comp_name,record_type,seq_number,record)')
    cur.executemany('INSERT INTO comps VALUES(?,?,?,?,?,?)', [
        (1,0,'RxDataTypeCollection',256,0,b''), (2,1,'Invented',256,0,b'type'),
        (3,2,'RxTypeMemberCollection',256,0,b''), (4,3,'Member',256,0,b'member')])
    monkeypatch.setattr(annotations.RxGeneric, 'from_bytes', lambda raw:
        SimpleNamespace(comment_id=5, record_buffer=b'xx'+struct.pack('<H',42)))
    assert annotations.member_map(cur) == {(5 << 16) | 42: ('MEMBER', 0)}
    db.close()


def test_owners_member_keys_and_unresolved_records(monkeypatch):
    from acd.l5x.elements import Tag
    db = sqlite3.connect(':memory:')
    cur = db.cursor()
    cur.execute('CREATE TABLE comps(object_id,parent_id,comp_name,record_type,seq_number,record)')
    cur.execute('CREATE TABLE comments(seq_number,parent,member_ref,tag_reference,record_string,record_type,rung_content)')
    def parse(raw):
        cip, comment, key = struct.unpack('<HHH', raw)
        return SimpleNamespace(cip_type=cip, comment_id=comment, record_buffer=b'xx'+struct.pack('<H', key))
    monkeypatch.setattr(annotations.RxGeneric, 'from_bytes', parse)
    def make_tag(oid, collection, comment, key):
        t = Tag('Tag', 'Shared', 'Base', 'DINT', None, 'Read/Write', None, None, 0, [])
        t._source_oid = oid
        cur.execute('INSERT INTO comps VALUES(?,?,?,?,?,?)',
                    (oid, collection, t.name, 256, oid, struct.pack('<HHH', 104, comment, key)))
        return t
    first = make_tag(1, 20, 3, 4)
    second = make_tag(2, 30, 5, 4)
    shared = [make_tag(i, i+50, 6, 9) for i in (3, 4)]
    root = SimpleNamespace(name='Root', tags=[], programs=[
        SimpleNamespace(name=str(t._source_oid), tags=[t]) for t in [first, second, *shared]])
    payload = '\n<&]]> \U0001f642\r\nend\n'
    for seq, (parent, key, ref, body, kind) in enumerate([
        (3, 4, '.5', payload, 3), (3, 4, '.5', 'repeat', 3),
        (5, 4, '.5', 'different scope', 3), (3, 8, '.5', 'wrong member', 3),
        (3, 4, '.!0000DEAD', 'missing member', 3), (6, 9, '.0', 'two owners', 3),
        (3, 4, '', 'short', 1), (3, 4, '', 'incompatible description', 2),
        (3, 4, '.0', '\x02', 3), (5, 4 << 16 | 107, '', payload, 1)
    ]):
        cur.execute('INSERT INTO comments VALUES(?,?,?,?,?,?,0)', (seq, parent << 16 | 104, key, ref, body, kind))
    annotations.materialize(root, cur)
    xml = ET.fromstring(first.to_xml())
    assert [(e.get('Operand'), e.text) for e in xml.findall('Comments/Comment')] == [
        ('.5', payload.replace('\r\n', '\n')), ('.5', 'repeat')]
    assert xml.find('Description') is None
    other = ET.fromstring(second.to_xml())
    assert other.findtext('Description') == '\n'+payload.replace('\r\n','\n')+'\n'
    assert other.findtext('Comments/Comment') == 'different scope'
    assert all(tag._comments == [] for tag in shared)
    assert sorted(problem[0] for problem in root._annotation_problems) == [
        'ambiguous owner', 'conflicting descriptions', 'invalid XML character', 'unclaimed source', 'unresolved member']
    db.close()


def test_raw_operand_and_text_decoding(monkeypatch):
    from acd.record.comments import CommentsRecord, FafaComents
    header = SimpleNamespace(record_type=5, seq_number=8, sub_record_length=0, parent=104)
    row = SimpleNamespace(identifier=64250, record=SimpleNamespace(record_buffer=b''))
    def parse(operand, text):
        body = bytes(16) + operand + b'\0\0' + text
        monkeypatch.setattr(FafaComents, 'from_bytes', lambda _: SimpleNamespace(header=header, body=body))
        return CommentsRecord.parse(row)
    assert parse('.\U0001f642'.encode('utf-16-le'), b' x \n\0')[3:7] == (' x \n', 5, 104, '.\U0001f642')
    assert parse(b'\x00\xd8', b'x\0') is None
    assert parse(b'X\0', b'\xff\0') is None
    assert parse(b'X\0', b'unterminated') is None


def test_duplicate_member_identity_remains_unresolved(monkeypatch):
    db = sqlite3.connect(':memory:')
    cur = db.cursor()
    cur.execute('CREATE TABLE comps(object_id,parent_id,comp_name,record_type,seq_number,record)')
    cur.executemany('INSERT INTO comps VALUES(?,?,?,?,?,?)', [
        (1,0,'RxDataTypeCollection',256,0,b''), (2,1,'Example',256,0,b''),
        (3,2,'RxTypeMemberCollection',256,0,b''), (4,3,'First',256,0,b''),
        (5,3,'Second',256,1,b'')])
    monkeypatch.setattr(annotations.RxGeneric, 'from_bytes', lambda raw:
        SimpleNamespace(comment_id=0, record_buffer=b'xx\x01\0'))
    mapping = annotations.member_map(cur)
    assert mapping == {1: None}
    assert annotations.operand('.!00000001', mapping) is None
    db.close()
