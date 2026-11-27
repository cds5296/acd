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


@pytest.mark.parametrize('legacy,expected', [(True, {0: 'First', 1: 'Second'}), (False, {})])
def test_bounded_legacy_indices(routine, legacy, expected):
    rows = [(oid, text, 0, 0, 0, 1) for oid, text in [(1, 'First'), (2, 'Second'), (9, 'Orphan')]]
    assert routine({}, rows, legacy=legacy)._rung_comments == expected


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
