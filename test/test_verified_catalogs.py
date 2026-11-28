import pytest
from acd.l5x.catalog_numbers import catalog_number_for_identity


@pytest.mark.parametrize('identity,expected', [((1,14,196),'5069-L306ER'),
    ((1,14,240),'5069-L350ERS2'), ((7,27,156),'EX260-SEN1/A')])
def test_verified_catalog_identity(identity, expected):
    assert catalog_number_for_identity(identity) == expected
