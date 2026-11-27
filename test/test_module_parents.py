from types import SimpleNamespace as N
import pytest
from acd.l5x.module_parents import parent_names


def test_parent_identity_is_scoped_module_comment_key():
    assert parent_names([('Root', N(cip_type=0x69, comment_id=8)), ('Bridge', N(cip_type=0x69, comment_id=9))]) == {8:'Root', 9:'Bridge'}


def test_ambiguous_identity_is_not_overwritten():
    with pytest.raises(ValueError):
        parent_names([('A', N(cip_type=0x69, comment_id=8)), ('B', N(cip_type=0x69, comment_id=8))])
