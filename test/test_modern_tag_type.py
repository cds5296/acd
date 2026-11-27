import struct
from acd.record.rx import RxGeneric


def test_modern_tag_collection_type_uses_tag_layout():
    raw = bytearray(82)
    struct.pack_into('<H', raw, 10, 0x6C)
    struct.pack_into('<I', raw, 42, 123)
    struct.pack_into('<I', raw, 50, 456)
    struct.pack_into('<II', raw, 74, 8, 1)
    tag = RxGeneric.from_bytes(raw)
    assert tag.main_record.data_type == 123
    assert tag.main_record.data_table_instance == 456
