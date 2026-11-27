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
    assert root.findtext('Data') == '[[4,5,6],[7,8,9]]'
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
