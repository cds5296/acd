import pytest
from acd.l5x.execution_class import source_class


@pytest.mark.parametrize('kind,version,size,offset', [('task',18,4372,4316), ('program',19,4194,139)])
def test_class_requires_version_size_and_known_value(kind, version, size, offset):
    data = bytearray(size)
    assert source_class(version, data, kind) == 'Standard'
    data[offset] = 6
    assert source_class(version, data, kind) == 'Safety'
    assert source_class(version + 1, data, kind) is None
    assert source_class(version, data[:-1], kind) is None
    data[offset] = 4
    with pytest.raises(ValueError):
        source_class(version, data, kind)
