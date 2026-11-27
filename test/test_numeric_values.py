import struct
import xml.etree.ElementTree as ET
import pytest
from acd.l5x.numeric_values import numeric_xml, dimensions


@pytest.mark.parametrize('kind,fmt,bits', [
    ('REAL', 'f', '00000080'), ('REAL', 'f', '01000000'),
    ('REAL', 'f', '0100803f'), ('REAL', 'f', 'ffff7f7f'),
    ('LREAL', 'd', '0000000000000080'), ('LREAL', 'd', '0100000000000000'),
])
def test_float_bits_preserved(kind, fmt, bits):
    raw = bytes.fromhex(bits)
    root = ET.fromstring('<Tag>' + numeric_xml(kind, None, raw) + '</Tag>')
    for value in (root.findtext('Data'), root.find('Data[@Format="Decorated"]/DataValue').get('Value')):
        assert struct.pack('<' + fmt, float(value)) == raw


def test_multidimensional_array_order():
    raw = struct.pack('<6i', 4, 5, 6, 7, 8, 9)
    root = ET.fromstring('<Tag>' + numeric_xml('DINT', '2 3', raw) + '</Tag>')
    assert root.findtext('Data') == '[4,5,6,7,8,9]'
    assert [e.get('Index') for e in root.findall('.//Element')] == [
        '[0,0]', '[0,1]', '[0,2]', '[1,0]', '[1,1]', '[1,2]']


@pytest.mark.parametrize('raw', [None, b'', b'123'])
def test_missing_values_never_zero_filled(raw):
    with pytest.raises(ValueError):
        numeric_xml('DINT', None, raw)


@pytest.mark.parametrize('text', ['-1', '1 0', '2,3,4,5', 'bad'])
def test_bad_dimensions(text):
    with pytest.raises(ValueError):
        dimensions(text)


def test_nonfinite_rejected():
    with pytest.raises(ValueError):
        numeric_xml('REAL', None, struct.pack('<f', float('nan')))

@pytest.mark.parametrize('text', ['2,,3', '2, ,3', ',2', '2,', '2\u00b23'])
def test_dimensions_reject_empty_or_invalid_extents(text):
    with pytest.raises(ValueError):
        dimensions(text)


def test_oversized_buffer_rejected():
    with pytest.raises(ValueError):
        numeric_xml('DINT', None, bytes(5))


def test_bool_array_length_does_not_establish_byte_per_element_layout():
    with pytest.raises(ValueError, match='Packed BOOL'):
        numeric_xml('BOOL', '4', bytes([1, 0, 0, 0]))


def test_materialization_resolves_unique_source_and_reports_omissions():
    import sqlite3
    from types import SimpleNamespace
    from loguru import logger
    from acd.l5x.numeric_values import materialize
    from acd.record.attribute_bounds import attribute_layout

    payload = struct.pack('<i', -17)
    raw = bytearray(82)
    struct.pack_into('<H', raw, 10, 0x6a)
    raw.extend(struct.pack('<II', 0x66, len(payload)) + payload)
    struct.pack_into('<II', raw, 74, len(raw) - 74, 1)

    def tag(name, instance):
        return SimpleNamespace(name=name, _data_table_instance=instance, tag_type='Base',
                               data_type='DINT', dimensions=None, _l5x_exclude=False)

    good, missing, duplicate = tag('Good', 1), tag('Missing', 2), tag('Duplicate', 3)
    good.dimensions = '1,1'
    program = SimpleNamespace(name='ProgramA', tags=[missing, duplicate])
    controller = SimpleNamespace(name='ControllerA', tags=[good], programs=[program])
    messages = []
    sink = logger.add(messages.append, format='{message}', level='WARNING')
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE comps(object_id INTEGER, record BLOB)')
        db.executemany('INSERT INTO comps VALUES (?, ?)', [(i, raw) for i in (1, 3, 3)])
        with attribute_layout('complete-counted'):
            materialize(controller, db.cursor())
    finally:
        logger.remove(sink)
        db.close()
    data = ET.fromstring('<Tag>' + good._source_value_xml + '</Tag>')
    assert good.dimensions == '1 1'
    assert data.findtext('Data') == '[-17]'
    assert data.find('.//Array').get('Dimensions') == '1,1'
    assert data.find('.//Element').attrib == {'Index': '[0,0]', 'Value': '-17'}
    for item in [missing, duplicate]:
        assert item._source_value_xml == ''
        assert any(f'ProgramA / {item.name} (instance {item._data_table_instance})' in msg
                   for msg in messages)
