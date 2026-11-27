import sqlite3
import struct
from types import SimpleNamespace as N
import pytest
from acd.l5x.elements import ModuleBuilder


@pytest.mark.parametrize('flags,expected', [(0, 'false'), (4, 'true'), (0x80, 'false'), (0x84, 'true')])
def test_module_inhibition_comes_from_configuration_word(monkeypatch, flags, expected):
    connection = sqlite3.connect(':memory:')
    connection.execute('CREATE TABLE comps(comp_name text, object_id int, parent_id int, seq_number int, record blob, record_type int)')
    connection.execute('CREATE TABLE comments(parent int, member_ref int, record_string text)')
    connection.execute("INSERT INTO comps VALUES ('CPU', 100, 1, 1, X'00', 256)")
    data = bytearray(946)
    data[20] = flags
    struct.pack_into('<I', data, 22, 9)
    struct.pack_into('<H', data, 26, 1)
    parsed = N(cip_type=0x69, record_format_version=166, comment_id=9, extended_records=[N(attribute_id=1, value=data)])
    monkeypatch.setattr('acd.l5x.elements.RxGeneric.from_bytes', lambda raw: parsed)
    module = ModuleBuilder(connection.cursor(), 100, {9:'CPU'}).build()
    assert module.inhibited == expected
    assert 'Inhibited="' + expected + '"' in module.to_xml()


def test_unverified_or_repeated_descriptor_cannot_supply_inhibition():
    from acd.l5x.module_inhibition import inhibition
    value = bytearray(946)
    rec = N(cip_type=0x69, record_format_version=166, extended_records=[N(attribute_id=1)])
    assert inhibition(rec,value[:-1]) is None
    rec.record_format_version=170
    assert inhibition(rec,value) is None
    rec.record_format_version=173
    data=bytearray(1504)
    assert inhibition(rec,data)=='false'
    data[20]=4
    assert inhibition(rec,data) is None
    rec.extended_records *= 2
    assert inhibition(rec,bytearray(1504)) is None
