import struct
from xml.etree import ElementTree as ET

from acd.api import ConvertAcdToL5x
from module_fixture import module_acd, module_descriptor
import pytest
from acd.l5x.safety_networks import decode, port_attributes


def test_network_identifiers_and_ports_remain_separate():
    data = bytearray(946)
    data[0x131:0x137] = (0x123456789abc).to_bytes(6, 'little')
    struct.pack_into('<HH', data, 0x36e, 1, 2)
    data[0x372:0x378] = (0xfedcba987654).to_bytes(6, 'little')
    module, ports = decode(166, data)
    assert module == '16#0000_1234_5678_9abc'
    assert ports == {2:'16#0000_fedc_ba98_7654'}
    assert 'SafetyNetwork="16#0000_fedc_ba98_7654"' in port_attributes('<Ports><Port Id="2"/></Ports>', ports)


def test_unverified_revision_and_malformed_table_are_not_guessed():
    assert decode(165, bytes(946)) == (None, {})
    data = bytearray(946)
    struct.pack_into('<H', data, 0x36e, 100)
    with pytest.raises(ValueError):
        decode(166, data)
    with pytest.raises(ValueError):
        port_attributes('<Ports/>', {2:'16#0000_1234_5678_9abc'})

def test_duplicate_snn_or_physical_port_is_ambiguous():
    data = bytearray(946)
    struct.pack_into('<H', data, 0x36e, 2)
    struct.pack_into('<H', data, 0x370, 2)
    struct.pack_into('<H', data, 0x378, 2)
    with pytest.raises(ValueError, match='Duplicate'):
        decode(166, data)
    with pytest.raises(ValueError, match='Duplicate'):
        port_attributes('<Ports><Port Id="2"/><Port Id="2"/></Ports>', {2:'16#0000_0000_0000_0001'})


@pytest.mark.parametrize('mode', ['compat', 'complete-counted'])
def test_conflicting_safety_networks_are_order_independent(tmp_path, mode):
    outcomes = []
    for index, values in enumerate([(1, 2), (2, 1)]):
        directory = tmp_path / str(index)
        directory.mkdir()
        source = module_acd(directory, [module_descriptor(network=n) for n in values])
        out = directory / 'preview.L5X'
        with pytest.warns(RuntimeWarning, match='Incomplete'):
            result = ConvertAcdToL5x(source, out, rx_attribute_layout=mode).extract()
        assert not result.complete
        assert ET.parse(out).find(".//Module[@Name='TestModule']") is None
        issues = [i for i in result.diagnostics if i['owner'] == 'TestModule']
        assert len(issues) == 1 and issues[0]['object_id'] == 0xc0debeef
        assert issues[0]['code'] == 'module-descriptor'
        assert 'duplicate' in issues[0]['reason'].lower()
        assert {'SafetyNetwork', 'Ports/Port/@SafetyNetwork'} <= set(issues[0]['affected_fields'])
        outcomes.append(issues)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize('mode', ['compat', 'complete-counted'])
@pytest.mark.parametrize('port_id', [1, 3])
def test_network_binds_only_to_existing_port(tmp_path, mode, port_id):
    data = module_descriptor(network=1)
    struct.pack_into('<HH', data, 0x36e, 1, port_id)
    data[0x372:0x378] = (2).to_bytes(6, 'little')
    source = module_acd(tmp_path, [data])
    out = tmp_path / 'preview.L5X'
    with pytest.warns(RuntimeWarning, match='Incomplete'):
        result = ConvertAcdToL5x(source, out, rx_attribute_layout=mode).extract()
    module = ET.parse(out).find(".//Module[@Name='TestModule']")
    assert module.get('SafetyNetwork') == '16#0000_0000_0000_0001'
    network = module.find("Ports/Port[@Id='1']").get('SafetyNetwork')
    assert network == ('16#0000_0000_0000_0002' if port_id == 1 else None)
    assert any(i['code'] == 'module-safety-network' and i['owner'] == 'TestModule'
               for i in result.diagnostics) == (port_id != 1)
