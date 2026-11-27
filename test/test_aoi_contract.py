import struct
import pytest
from acd.l5x.aoi_contract import execution_word, ordered_declarations


def declaration(scope, ordinal):
    return bytes(10) + struct.pack('<II', scope, ordinal)


def test_declaration_order_uses_own_identity_not_input_order():
    a, b = declaration(0x70168, 5), declaration(0x70168, 2)
    assert ordered_declarations([(1, a), (9, b)]) == [(9, b), (1, a)]


@pytest.mark.parametrize('rows', [[(1, b'x')], [(1, declaration(3, 1)), (2, declaration(3, 1))], [(1, declaration(3, 1)), (2, declaration(4, 2))]])
def test_ambiguous_declarations_fail(rows):
    with pytest.raises(ValueError):
        ordered_declarations(rows)


def test_prescan_word_is_source_backed():
    assert execution_word({0x194: struct.pack('<I', 1)}, 0x194) == 'true'
    assert execution_word({}, 0x194) is None
    with pytest.raises(ValueError):
        execution_word({0x194: b'\x02\x00\x00\x00'}, 0x194)

def test_normal_converter_identifies_unreadable_parameter(monkeypatch, tmp_path):
    from pathlib import Path
    from acd.api import ConvertAcdToL5x
    from acd.l5x.elements import ParameterBuilder
    def bad_alias(builder):
        raise ValueError('Invalid AOI parameter alias target')
    monkeypatch.setattr(ParameterBuilder, 'build', bad_alias)
    source = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    with pytest.warns(RuntimeWarning):
        result = ConvertAcdToL5x(source, tmp_path / 'preview.L5X',
                                 rx_attribute_layout='complete-counted').extract()
    items = [i for i in result.diagnostics if i['code']=='aoi-declaration']
    assert items and all(i['owner'].startswith('AddOnInstruction/') and i['object_id'] for i in items)
    assert all(i['reason']=='Invalid AOI parameter alias target' for i in items)
