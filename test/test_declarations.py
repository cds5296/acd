from types import SimpleNamespace
import pytest
from acd.l5x.declarations import unique_tags


def tag(data_type=None, alias=None, oid=3):
    return SimpleNamespace(name='Value', data_type=data_type, alias_for=alias,
                           tag_type='Alias' if alias else 'Base', dimensions=None, _data_table_instance=oid)


def test_full_tag_and_alias_win_over_stub_in_either_order():
    for full in (tag('DINT'), tag(alias='Other.0')):
        stub = tag()
        assert unique_tags([full, stub]) == [full]
        assert unique_tags([stub, full]) == [full]


def test_conflicting_values_are_not_silently_selected():
    with pytest.raises(ValueError):
        unique_tags([tag('DINT'), tag('DINT', oid=4)])


def test_deleted_udt_child_does_not_shift_member_definition_association(monkeypatch):
    import sqlite3
    import struct
    from acd.l5x.elements import DataTypeBuilder, MemberBuilder
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE comps(comp_name text, object_id int, parent_id int, seq_number int, record_type int, record blob)')
    connection.execute('CREATE TABLE comments(parent int, member_ref int, record_string text)')
    connection.executemany('INSERT INTO comps VALUES (?,?,?,?,?,?)', [
        ('Pair',100,0,1,256,b'Type'), ('Members',101,100,2,256,b'Collection'),
        ('First',1,101,3,256,b'A'), ('Deleted',2,101,4,512,b'X'),
        ('Second',3,101,5,256,b'B')])
    first, second = b'A'*128, b'B'*128
    parsed = SimpleNamespace(comment_id=0, cip_type=0x6C, extended_records=[
        SimpleNamespace(attribute_id=k, value=v) for k,v in
        [(0x64,struct.pack('<I',2)), (0x6E,first), (0x6F,second)]])
    monkeypatch.setattr('acd.l5x.elements.RxGeneric.from_bytes', lambda raw: parsed)
    def member(builder):
        name = connection.execute('SELECT comp_name FROM comps WHERE object_id=?', (builder._object_id,)).fetchone()[0]
        return SimpleNamespace(name=name, record=builder.record)
    monkeypatch.setattr(MemberBuilder, 'build', member)
    result = DataTypeBuilder(connection.cursor(), 100).build()
    assert [(m.name, m.record) for m in result.members] == [('First',first), ('Second',second)]


@pytest.mark.parametrize('field', ['constant', 'external_access', 'radix', 'dimensions',
                                   'alias_for', 'produce_info', 'consume_info', '_comments'])
def test_different_complete_metadata_is_a_conflict_in_both_orders(field):
    first = tag('DINT')
    second = tag('DINT')
    setattr(second, field, 'different')
    for declarations in ([first, second], [second, first]):
        with pytest.raises(ValueError, match='Value'):
            unique_tags(declarations)


def test_live_declarations_are_selected_within_each_collection():
    import sqlite3
    from acd.l5x.declarations import live_tag_records
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE comps(comp_name,object_id,parent_id,record_type,seq_number)')
        db.executemany('INSERT INTO comps VALUES(?,?,?,?,?)', [
            ('X', 1, 100, 256, 1), ('X', 2, 100, 512, 2), ('Y', 3, 100, 256, 3),
            ('X', 4, 200, 512, 4), ('X', 5, 200, 256, 5)])
        assert live_tag_records(db.cursor(), 100) == [('X',1,100,256), ('Y',3,100,256)]
        assert live_tag_records(db.cursor(), 200) == [('X',5,200,256)]


def test_program_and_controller_skip_deleted_tags_before_decoding(tmp_path, monkeypatch):
    from pathlib import Path
    from acd.l5x.elements import ControllerBuilder, TagBuilder
    from acd.l5x.export_l5x import ExportL5x

    fixture = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    export = ExportL5x(fixture, str(tmp_path))
    try:
        cur = export._cur
        collections = cur.execute(
            "SELECT t.object_id FROM comps t JOIN comps owner ON t.parent_id=owner.object_id "
            "LEFT JOIN comps p ON owner.parent_id=p.object_id WHERE t.comp_name='RxTagCollection' "
            "AND (owner.parent_id=0 OR p.comp_name='RxProgramCollection') "
            "AND t.record_type!=512 AND owner.record_type!=512"
        ).fetchall()
        assert len(collections) >= 2
        seen = set()
        dead = set()
        for i, (parent,) in enumerate(collections):
            previous = cur.execute("SELECT object_id,comp_name FROM comps WHERE parent_id=? "
                                   "AND record_type=256 LIMIT 1", (parent,)).fetchone()
            oid = -i-1
            dead.add(oid)
            name = previous[1] if previous else 'DeletedExample'
            cur.execute("INSERT INTO comps VALUES (?,?,?,?,?,?)", (oid,parent,name,0,512,b''))
        build = TagBuilder.build
        def inspect(builder):
            assert builder._object_id not in dead
            seen.add(builder._object_id)
            return build(builder)
        monkeypatch.setattr(TagBuilder, 'build', inspect)
        controller = ControllerBuilder(cur).build()
        assert seen and controller.tags and controller.programs
    finally:
        export._db.close()
