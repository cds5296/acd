import xml.etree.ElementTree as ET
import pytest
from acd.l5x.controller_fields import description_xml, topology


def test_description_preserves_text_and_cdata_delimiter():
    text = 'Controller\n\ncomment ]]> tail'
    assert ET.fromstring(description_xml([('', text), ('[0]', 'Operand')])).text == text


def test_competing_descriptions_are_not_selected_by_length():
    with pytest.raises(ValueError):
        description_xml([('', 'first'), ('', 'second')])


def test_topology_does_not_invent_unknown_enums():
    assert topology(b'\x02\x00') == 'A1/A2: Linear/DLR'
    assert topology(None) is None
    with pytest.raises(ValueError):
        topology(b'\x03\x00')
