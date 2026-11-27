import struct
import xml.etree.ElementTree as ET

import pytest

from acd.l5x.elements import TagBuilder
from acd.record.attribute_bounds import attribute_layout
from acd.record.rx import RxGeneric
from test_rx import _database, _insert


def test_modern_tag_collection_type_uses_tag_layout():
    raw = bytearray(82)
    struct.pack_into('<H', raw, 10, 0x6C)
    struct.pack_into('<I', raw, 42, 123)
    struct.pack_into('<I', raw, 50, 456)
    struct.pack_into('<II', raw, 74, 8, 1)
    tag = RxGeneric.from_bytes(raw)
    assert tag.main_record.data_type == 123
    assert tag.main_record.data_table_instance == 456


def _modern_alias(cip_type, path, radix):
    raw = bytearray(82)
    struct.pack_into('<H', raw, 10, cip_type)
    struct.pack_into('<H', raw, 46, radix)
    for key, value in [(1, b'\x05\x00Alias'), (0x65, path)]:
        raw.extend(struct.pack('<II', key, len(value)) + value)
    struct.pack_into('<II', raw, 74, len(raw) - 74, 2)
    return raw


@pytest.mark.parametrize('cip_type,radix', [(0x68, 4), (0x6b, 0), (0x6c, 0)])
def test_final_alias_attribute_uses_target_and_source_radix(cip_type, radix):
    db, cursor = _database()
    try:
        path = '@00000010@.3'.encode('utf-16-le')
        _insert(cursor, 16, 'Target', b'')
        _insert(cursor, 1, 'RowAlias', _modern_alias(cip_type, path, radix))
        with attribute_layout('complete-counted'):
            xml = ET.fromstring(TagBuilder(cursor, 1).build().to_xml())
        expected = dict(Name='Alias', TagType='Alias', AliasFor='Target.3',
                        ExternalAccess='Read/Write')
        if radix:
            expected['Radix'] = 'Decimal'
        assert xml.attrib == expected
        assert xml.find('Data') is None
    finally:
        db.close()


@pytest.mark.parametrize('path', [b'', b'x', '@00000020@.7'.encode('utf-16-le'),
                                  'a\0b'.encode('utf-16-le')])
def test_invalid_modern_alias_fails(path):
    db, cursor = _database()
    try:
        _insert(cursor, 1, 'Alias', _modern_alias(0x6c, path, 0))
        with attribute_layout('complete-counted'), pytest.raises(ValueError):
            TagBuilder(cursor, 1).build()
    finally:
        db.close()


def test_unreadable_record_preserves_tag_builder_fallback():
    db, cursor = _database()
    try:
        _insert(cursor, 1, 'Unreadable', b'')
        tag = TagBuilder(cursor, 1).build()
        assert (tag.name, tag.tag_type, tag.alias_for) == ('Unreadable', 'Base', None)
    finally:
        db.close()
