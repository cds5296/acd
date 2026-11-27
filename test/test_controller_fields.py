import xml.etree.ElementTree as ET
import pytest
from acd.l5x.controller_fields import description_xml, topology


def test_description_preserves_text_and_cdata_delimiter():
    text = 'Controller\n\ncomment ]]> tail'
    assert ET.fromstring(description_xml([('', text), ('[0]', 'Operand')])).text == text


def test_competing_descriptions_are_not_selected_by_length():
    with pytest.raises(ValueError):
        description_xml([('', 'first'), ('', 'second')])


def test_topology_does_not_invent_unknown_enums():
    assert topology(b'\x02\x00') == 'A1/A2: Linear/DLR'
    assert topology(None) is None
    with pytest.raises(ValueError):
        topology(b'\x03\x00')

@pytest.mark.parametrize('mode', [None, b'\x00\x00'])
def test_absent_or_zero_topology(mode):
    assert topology(mode) is None


@pytest.mark.parametrize('raw', [b'\x01\x00', b'\x02', b'\x02\x00\x00'])
def test_unverified_topology_layouts(raw):
    with pytest.raises(ValueError):
        topology(raw)


def test_absent_description_has_no_element():
    assert description_xml([]) == ''
    assert description_xml([('[0]', 'operand')]) == ''


@pytest.mark.parametrize('name', ['ACDTestsWithAOI', 'ACDTestsEmptyRedundant'])
def test_controller_description_against_public_export(tmp_path, name):
    from pathlib import Path
    from acd.api import ConvertAcdToL5x
    resources = Path(__file__).resolve().parents[1] / 'resources'
    result = tmp_path / 'result.L5X'
    ConvertAcdToL5x(resources / (name + '.ACD'), result).extract()
    native = ET.parse(resources / (name + '.L5X')).find('Controller/Description')
    actual = ET.parse(result).find('Controller/Description')
    # Both public references place a newline outside each end of the CDATA.
    assert native.text == '\n' + actual.text + '\n'



@pytest.mark.parametrize('modes,expected,problem', [
    ([b'\x02\x00'], 'A1/A2: Linear/DLR', None),
    ([b'\x01\x00'], None, 'Unverified'),
    ([b'\x02\x00', b'\x00\x00'], None, 'Conflicting'),
])
def test_builder_scopes_description_and_topology(tmp_path, modes, expected, problem):
    import struct
    from pathlib import Path
    from acd.l5x.elements import ControllerBuilder
    from acd.l5x.export_l5x import ExportL5x
    from acd.record.rx import RxGeneric

    acd = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    export = ExportL5x(acd, str(tmp_path / 'db'))
    try:
        cur = export._cur
        oid, raw = cur.execute('SELECT object_id,record FROM comps WHERE parent_id=0 AND record_type=256').fetchone()
        root = RxGeneric.from_bytes(raw)
        parent = (root.comment_id << 16) | root.cip_type
        cur.execute('DELETE FROM comments WHERE parent=?', (parent,))
        cur.executemany('INSERT INTO comments(parent,record_type,member_ref,rung_content,tag_reference,record_string) '
                        'VALUES (?,?,?,?,?,?)', [(parent, 1, 4, 0, '', 'Member'),
                                                 (parent, 1, 0, 7, '', 'Rung'),
                                                 (parent, 1, 0, 0, '', 'Root')])
        attrs = [(a.attribute_id, bytes(a.value)) for a in root.extended_records if a.attribute_id != 0x7c]
        attrs.extend((0x7c, mode) for mode in modes)
        raw = bytearray(raw[:82])
        for key, value in attrs:
            raw.extend(struct.pack('<II', key, len(value)) + value)
        raw.extend(struct.pack('<II', 0xff, 4))
        struct.pack_into('<II', raw, 74, len(raw) - 74, len(attrs) + 1)
        cur.execute('UPDATE comps SET record=? WHERE object_id=?', (raw, oid))
        controller = ControllerBuilder(cur).build()
        xml = ET.fromstring(controller.to_xml())
        assert xml.findtext('Description') == 'Root'
        assert xml.get('EtherNetIPMode') == expected
        if problem:
            assert len(controller._metadata_problems) == 1
            assert problem in controller._metadata_problems[0]
        else:
            assert controller._metadata_problems == []
    finally:
        export._db.close()
