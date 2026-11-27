from types import SimpleNamespace as N
import pytest
from acd.l5x.aoi_contract import declaration_metadata


def record(size,revision=63,cls=0x338):
    return N(cip_type=cls,record_format_version=revision,extended_records=[N(attribute_id=1,value=bytes(size))])


def test_complete_scoped_declaration_metadata_only():
    assert len(declaration_metadata(record(590)))==590
    for bad in [record(539),record(591),record(590,62),record(590,cls=0x68)]:
        with pytest.raises(ValueError):
            declaration_metadata(bad)
    duplicate=record(590)
    duplicate.extended_records*=2
    with pytest.raises(ValueError,match='ambiguous'):
        declaration_metadata(duplicate)
