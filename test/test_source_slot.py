import pytest
from xml.etree import ElementTree as ET
from acd.api import ConvertAcdToL5x
from module_fixture import module_acd, module_descriptor


@pytest.mark.parametrize('mode', ['compat', 'complete-counted'])
@pytest.mark.parametrize('slot,expected', [(0, '0'), (3, '3'), (0xffffffff, None)])
def test_slot_address_through_converter(tmp_path, mode, slot, expected):
    source = module_acd(tmp_path, [module_descriptor(slot)])
    output = tmp_path / 'preview.L5X'
    with pytest.warns(RuntimeWarning, match='Incomplete'):
        result = ConvertAcdToL5x(source, output, rx_attribute_layout=mode).extract()
    port = ET.parse(output).find(".//Module[@Name='TestModule']/Ports/Port[@Id='1']")
    assert port is not None and port.get('Address') == expected
    diagnostics = [i for i in result.diagnostics if i['code'] == 'module-port-address'
                   and i['owner'] == 'TestModule']
    assert [i['port_id'] for i in diagnostics] == ([1] if expected is None else [])
