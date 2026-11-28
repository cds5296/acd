from xml.etree import ElementTree as ET
from acd.api import ConvertAcdToL5x
from acd.l5x.completeness import IncompleteExportError
from module_fixture import module_acd, module_descriptor


import pytest
from acd.l5x.keying import electronic_keying


@pytest.mark.parametrize('unrelated,mask,expected', [(4, 31, 'CompatibleModule'), (0, 0, 'Disabled'), (255, 0, 'Disabled')])
def test_mask_is_independent_of_first_byte(unrelated, mask, expected):
    assert electronic_keying(bytes([unrelated]) + bytes(9) + bytes([mask, 0])) == expected


@pytest.mark.parametrize('raw', [bytes(11), bytes(10) + b'\x10\x00'])
def test_unknown_keying_is_not_guessed(raw):
    with pytest.raises(ValueError):
        electronic_keying(raw)


@pytest.mark.parametrize('mode', ['compat', 'complete-counted'])
@pytest.mark.parametrize('kind', ['duplicate', 'reverse', 'last', 'short', 'short-layout'])
def test_invalid_module_descriptor_is_diagnosed_through_converter(tmp_path, mode, kind):
    a = module_descriptor(mask=0)
    b = module_descriptor(mask=31)
    descriptors = {'duplicate': [a, b], 'reverse': [b, a], 'last': [a, b],
                   'short': [a[:11]], 'short-layout': [a[:-1]]}
    source = module_acd(tmp_path, descriptors[kind], final_padding=kind != 'last')
    out = tmp_path / 'preview.L5X'
    with pytest.warns(RuntimeWarning, match='Incomplete'):
        result = ConvertAcdToL5x(source, out, rx_attribute_layout=mode).extract()
    assert not result.complete
    issues = [d for d in result.diagnostics if d['owner'] == 'TestModule'
              and d['code'] == 'module-descriptor']
    assert len(issues) == 1
    assert ('truncated' if kind.startswith('short') else 'duplicate') in issues[0]['reason'].lower()
    assert ET.parse(out).find(".//Module[@Name='TestModule']") is None
    strict = tmp_path / 'strict.L5X'
    with pytest.raises(IncompleteExportError):
        ConvertAcdToL5x(source, strict, rx_attribute_layout=mode, allow_incomplete=False).extract()
    assert not strict.exists()
