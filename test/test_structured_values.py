import struct
import xml.etree.ElementTree as ET

import pytest

from acd.l5x.values import Layout, LayoutMember, UnresolvedValue, ValueEncoder


def encoder():
    layouts = {
        'TEXT': Layout('Text', 12, [], 8),
        'PAIR': Layout('Pair', 8, [
            LayoutMember('Storage', 'DINT', 0, None, (), True),
            LayoutMember('Ready', 'BOOL', 0, 31, (), False),
            LayoutMember('Value', 'REAL', 4, None, (), False)]),
    }
    return ValueEncoder(layouts, {})


def test_hidden_storage_is_retained_without_duplicate_bit_slot():
    xml = encoder().encode('Pair', struct.pack('<If', 0x80000000, -0.0))
    root = ET.fromstring('<Tag>' + xml + '</Tag>')
    assert root.find('Data').text == '[2147483648,-0.00000000e+000]'.replace('2147483648', '-2147483648')
    members = root.findall("Data[@Format='Decorated']/Structure/DataValueMember")
    assert [(m.get('Name'), m.get('Value')) for m in members] == [('Ready', '1'), ('Value', '-0')]


def test_string_retains_inactive_capacity_and_escapes():
    xml = encoder().encode('Text', struct.pack('<i', 3) + b"a$'\x00TAIL")
    root = ET.fromstring('<Tag>' + xml + '</Tag>')
    assert root.find("Data[@Format='L5K']").text == "[3,'a$$$'$00TAIL']"
    assert root.find("Data[@Format='String']").text == "'a$$$''"


@pytest.mark.parametrize('length', [-1, 9])
def test_invalid_string_length_is_not_clamped(length):
    with pytest.raises(UnresolvedValue):
        encoder().encode('Text', struct.pack('<i', length) + bytes(8))


@pytest.mark.parametrize('raw', [bytes(7), bytes(9)])
def test_layout_bounds_are_exact(raw):
    with pytest.raises(UnresolvedValue):
        encoder().encode('Pair', raw)


def test_packed_boolean_array_preserves_last_bit():
    xml = encoder().encode('BOOL', struct.pack('<II', 0x80000001, 1), '33')
    values = ET.fromstring('<Tag>' + xml + '</Tag>').findall("Data[@Format='Decorated']/Array/Element")
    assert len(values) == 33
    assert [i for i, v in enumerate(values) if v.get('Value') == '1'] == [0, 31, 32]


def test_unknown_configuration_never_becomes_zero():
    with pytest.raises(UnresolvedValue, match='not decoded'):
        encoder().encode('MESSAGE', bytes(100))


def test_nested_boolean_array_uses_packed_member_extent():
    value_encoder = ValueEncoder({'FLAGS': Layout('Flags', 8, [
        LayoutMember('Flags', 'BOOL', 4, None, (32,), False)])}, {})
    root = ET.fromstring('<Tag>' + value_encoder.encode('Flags', struct.pack('<II', 0, 0x80000000)) + '</Tag>')
    values = root.findall("Data[@Format='Decorated']/Structure/ArrayMember/Element")
    assert len(values) == 32
    assert values[-1].get('Value') == '1'


def test_member_cannot_read_the_next_array_element():
    value_encoder = ValueEncoder({'BAD': Layout('Bad', 4, [
        LayoutMember('Outside', 'DINT', 4, None, (), False)])}, {})
    with pytest.raises(UnresolvedValue, match='containing layout'):
        value_encoder.encode('Bad', bytes(8), '2')


def test_nested_arrays_keep_structure_index_and_member_order():
    layouts = {
        'ROW': Layout('Row', 4, [LayoutMember('Cell', 'INT', 0, None, (2,), False)]),
        'GRID': Layout('Grid', 8, [LayoutMember('Rows', 'Row', 0, None, (2,), False)])
    }
    root = ET.fromstring('<Tag>' + ValueEncoder(layouts, {}).encode('Grid', struct.pack('<4h', 3, -7, 20, 8)) + '</Tag>')
    assert root.findtext("Data[@Format='L5K']") == '[[[[3,-7]],[[20,8]]]]'
    rows = root.findall(".//ArrayMember[@Name='Rows']/Element")
    assert [row.get('Index') for row in rows] == ['[0]', '[1]']
    assert [[(cell.get('Index'), cell.get('Value')) for cell in row.findall(".//ArrayMember[@Name='Cell']/Element")]
            for row in rows] == [[('[0]', '3'), ('[1]', '-7')], [('[0]', '20'), ('[1]', '8')]]


def test_string_capacity_cannot_cross_into_the_next_array_element():
    bad = ValueEncoder({'TEXT': Layout('Text', 6, [], 4)}, {})
    with pytest.raises(UnresolvedValue, match='capacity'):
        bad.encode('Text', bytes(12), '2')


def test_string_escapes_active_bytes_and_preserves_nonzero_inactive_bytes():
    xml = encoder().encode('Text', struct.pack('<i', 6) + bytes([0x24, 0x27, 0x0D, 0x0A, 0xFF, 0, 0x41, 0x42]))
    root = ET.fromstring('<Tag>' + xml + '</Tag>')
    assert root.findtext("Data[@Format='String']") == "'$$$'$r$l$FF$00'"
    assert root.findtext("Data[@Format='L5K']") == "[6,'$$$'$r$l$FF$00AB']"
    root = ET.fromstring('<Tag>' + encoder().encode('Text', bytes(4) + b'abcdefgh') + '</Tag>')
    assert root.findtext("Data[@Format='String']") == "''"
    assert root.findtext("Data[@Format='L5K']") == "[0,'abcdefgh']"


def test_bit_declaration_uses_unique_covering_storage():
    from types import SimpleNamespace as N
    from acd.l5x.values import repair_bit_targets
    flag = N(name='Ready', data_type='BOOL', dimension=1)
    dt = N(name='Flags', cls='User', members=[flag])
    host = LayoutMember('Backing', 'DINT', 0, None, (), True)
    layout = Layout('Flags', 4, [host, LayoutMember('Ready', 'BOOL', 2, 3, (), False)])
    problems = []
    repair_bit_targets({'FLAGS': dt}, {'FLAGS': layout}, problems)
    assert (flag.data_type, flag.target, flag.bit_number, flag.dimension) == ('BIT', 'Backing', 19, 0)
    assert not problems
    layout.members.append(LayoutMember('Other', 'INT', 2, None, (), True))
    repair_bit_targets({'FLAGS': dt}, {'FLAGS': layout}, problems)
    assert flag.target is None and flag.bit_number is None
    assert problems[0]['code'] == 'bit-backing' and problems[0]['owner'] == 'Flags/Ready'


def test_hidden_name_alone_does_not_drop_a_physical_slot():
    member = LayoutMember('__l0123456789ABCDEF', 'DINT', 0, None, (), True)
    layouts = {'STRUCT': Layout('Struct', 4, [member])}
    raw = struct.pack('<i', 8)
    assert '<![CDATA[[8]]]>' in ValueEncoder(layouts, {}).encode('Struct', raw)
    with pytest.raises(UnresolvedValue, match='AOI'):
        ValueEncoder(layouts, {}, {'STRUCT'}).encode('Struct', raw)


def test_duplicate_type_layout_does_not_overwrite_a_valid_neighbor(tmp_path):
    from acd.l5x.values import load_layouts
    source = tmp_path / 'TagInfo.XML'
    source.write_text('<Root><DataTypes><DataType Name="X" Size="4"/><DataType Name="x" Size="8"/>'
                      '<DataType Name="Y" Size="1"/></DataTypes></Root>')
    issues = []
    assert list(load_layouts(source, issues)) == ['Y']
    assert issues[0]['code'] == 'tag-layout' and issues[0]['owner'] == 'x'


@pytest.mark.parametrize('kind, radix, raw', [('DINT', 'ASCII', bytes(4)), ('REAL', 'Hex', bytes(4))])
def test_explicit_unhandled_member_radix_is_reported(kind, radix, raw):
    from types import SimpleNamespace as N
    layout = Layout('Row', 4, [LayoutMember('Cell', kind, 0, None, (), False)])
    declaration = N(members=[N(name='Cell', radix=radix)])
    decoder = ValueEncoder({'ROW': layout}, {'ROW': declaration})
    with pytest.raises(UnresolvedValue, match=f'{kind} display radix: {radix}'):
        decoder.encode('Row', raw)
