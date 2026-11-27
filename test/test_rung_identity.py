import sqlite3
import struct
from types import SimpleNamespace

import pytest
from acd.l5x.elements import AoiBuilder, RoutineBuilder
from acd.record.rx import LegacyRxGeneric, RxGeneric
from acd.record.sbregion import SbRegionRecord, FafaSbregions


@pytest.fixture
def routine(monkeypatch):
    db = sqlite3.connect(':memory:')
    cur = db.cursor()
    cur.executescript("""
        CREATE TABLE comps(comp_name,object_id,parent_id,record);
        CREATE TABLE region_map(object_id,parent_id,unknown);
        CREATE TABLE rungs(object_id,rung);
        CREATE TABLE comments(object_id,record_string,parent,record_type,rung_content,member_ref);
        INSERT INTO comps VALUES('Logic',100,0,X'00');
        INSERT INTO region_map VALUES(102,100,1),(101,100,0);
        INSERT INTO rungs VALUES(101,'NOP();'),(102,'AFI();');
    """)
    parent = (5 << 16) | 0x6d
    member = (77 << 16) | 0x6d

    def build(keys, rows, legacy=False, type_id=1, aoi=False):
        header = bytearray(50)
        struct.pack_into('<H', header, 2, 77)
        struct.pack_into('<H', header, 48, type_id)
        parsed = object.__new__(LegacyRxGeneric) if legacy else SimpleNamespace()
        parsed.comment_id, parsed.cip_type, parsed.record_buffer = 5, 0x6d, header
        monkeypatch.setattr(RxGeneric, 'from_bytes', lambda raw: parsed)
        cur.executemany('INSERT INTO comments VALUES(?,?,?,?,?,?)',
                        [(oid, text, parent + dp, rt, (key << 16) | 1, member + dm)
                         for oid, text, key, dp, dm, rt in rows])
        if aoi:
            cur.executescript("""
                CREATE TABLE nameless(parent_id,record);
                INSERT INTO comps VALUES('Instruction',1,0,X'00'),('RxRoutineCollection',2,1,X'00');
                UPDATE comps SET parent_id=2 WHERE object_id=100;
            """)
            return AoiBuilder(cur, 1, keys).build().routines[0]
        return RoutineBuilder(cur, 100, keys).build()
    yield build
    db.close()


def test_second_rung_only_in_own_scope(routine):
    result = routine({101: 10, 102: 12}, [
        (1, 'Second', 12, 0, 0, 2), (1, 'Wrong parent', 10, 1, 0, 1),
        (1, 'Wrong routine', 10, 0, 65536, 1), (1, 'Orphan', 14, 0, 0, 1),
    ], type_id=0)
    assert result.type == 'RLL'
    assert result.rungs == ['NOP();', 'AFI();']
    assert result._rung_comments == {1: 'Second'}


@pytest.mark.parametrize('keys,key,expected', [
    ({101: 10, 102: 12}, 10, {0: 'Text'}),
    ({101: 10, 102: 12}, 15, {}),
    ({101: 10, 102: 10}, 10, {}),
    ({}, 10, {}),
])
def test_single_comment_requires_unique_source_key(routine, keys, key, expected):
    assert routine(keys, [(1, 'Text', key, 0, 0, 1)])._rung_comments == expected


def test_competing_texts_never_pick_a_winner(routine):
    rows = [(1, text, 12, 0, 0, 1) for text in ('First', 'Second')]
    assert routine({102: 12}, rows)._rung_comments == {}


@pytest.mark.parametrize('legacy', [True, False])
def test_distinct_object_ids_do_not_establish_rung_positions(routine, legacy):
    rows = [(oid, text, key, 0, 0, 1) for oid, text, key in
            [(1, 'Orphan', 7), (2, 'First', 10), (9, 'Second', 12)]]
    assert routine({101: 10, 102: 12}, rows, legacy=legacy)._rung_comments == {0: 'First', 1: 'Second'}


@pytest.mark.parametrize('legacy', [True, False])
def test_missing_source_keys_never_route_by_object_id(routine, legacy):
    rows = [(1, 'Orphan', 7, 0, 0, 1), (2, 'Other', 8, 0, 0, 1)]
    assert routine({}, rows, legacy=legacy)._rung_comments == {}


def test_conflicting_ast_records_stay_unresolved(monkeypatch):
    dat = SimpleNamespace(identifier=64250, record=SimpleNamespace(record_buffer=b''))
    keys = {}
    for oid, key in [(101, 8), (101, 9), (101, 8), (102, 8)]:
        parsed = SimpleNamespace(header=SimpleNamespace(language_type='REGION AST', identifier=oid),
                                 record_buffer=struct.pack('<H', key) + bytes(16))
        monkeypatch.setattr(FafaSbregions, 'from_bytes', lambda raw: parsed)
        assert SbRegionRecord.parse(dat, {}, keys) is None
    assert keys == {101: None, 102: 8}


def test_aoi_forwards_rung_identities(routine):
    result = routine({101: 10, 102: 12}, [(1, 'AOI second', 12, 0, 0, 1)], aoi=True)
    assert result._rung_comments == {1: 'AOI second'}


def test_multiline_comment_survives_windows_text_output(routine, tmp_path):
    import xml.etree.ElementTree as ET
    source = ' leading\r\n\r\nline ]]> end\r\n '
    result = routine({102: 12}, [(1, source, 12, 0, 0, 1)])
    path = tmp_path / 'routine.xml'
    with path.open('w', encoding='utf-8', newline='\r\n') as out:
        out.write(result.to_xml())
    assert ET.parse(path).findtext('RLLContent/Rung[@Number="1"]/Comment') == ' leading\n\nline ]]> end\n '


def test_link_only_public_project_keeps_scoped_comment(tmp_path):
    from pathlib import Path
    import xml.etree.ElementTree as ET
    from acd.api import ConvertAcdToL5x

    resources = Path(__file__).resolve().parents[1] / 'resources'
    result = tmp_path / 'output.L5X'
    ConvertAcdToL5x(resources / 'ACDTestsWithAOI.ACD', result).extract()
    path = "Controller/Programs/Program[@Name='MainProgram']/Routines/Routine[@Name='MainRoutine']/RLLContent/Rung"
    expected = ET.parse(resources / 'ACDTestsWithAOI.L5X').findall(path)
    actual = ET.parse(result).findall(path)
    assert [(r.get('Number'), r.findtext('Comment')) for r in actual if r.find('Comment') is not None] == [
        (r.get('Number'), r.findtext('Comment')[1:-1])
        for r in expected if r.find('Comment') is not None]


@pytest.mark.parametrize('corrupt_extent', [False, True])
def test_link_joins_owner_and_rung_and_rejects_ambiguity(tmp_path, corrupt_extent, routine):
    from acd.record.rung_links import supplement_rung_keys

    flag = bytes.fromhex('000002000001')
    rows = [(flag, 12, 100, 102, 0xffffffff), (flag, 10, 555, 101, 102),
            (flag, 8, 100, 103, 101), (flag, 9, 100, 103, 101)]
    raw = bytearray(128)
    raw[40:52] = bytes.fromhex('81000000 1600ffff 12001000')
    struct.pack_into('<II', raw, 8, 127 + 22 * len(rows), 60)
    struct.pack_into('<I', raw, 24, len(rows) + int(corrupt_extent))
    raw[60:62] = raw[96:98] = b'\xfe\xfe'
    struct.pack_into('<I', raw, 78, 96)
    struct.pack_into('<I', raw, 98, 32)
    for row in rows:
        raw.extend(struct.pack('<6sIIII', *row))
    path = tmp_path / 'links.dat'
    path.write_bytes(raw)
    db = sqlite3.connect(':memory:')
    try:
        db.executescript('CREATE TABLE region_map(parent_id,object_id); CREATE TABLE rungs(object_id);'
                         'INSERT INTO region_map VALUES(100,101),(100,102),(100,103);'
                         'INSERT INTO rungs VALUES(101),(102),(103);')
        keys = {}
        supplement_rung_keys(path, db.cursor(), keys)
    finally:
        db.close()
    assert keys == ({} if corrupt_extent else {102: 12, 103: None})
    result = routine(keys, [(1, 'Second', 12, 0, 0, 1)])
    assert result._rung_comments == ({} if corrupt_extent else {1: 'Second'})
