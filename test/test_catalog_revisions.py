import json
import pytest
from acd.l5x.catalog_numbers import CatalogError, catalog_number_for_identity, load_external_catalog


def test_revision_override_retains_triple_fallback():
    table = {(9, 8, 7): 'Generic', (9, 8, 7, 2): 'RevisionTwo'}
    assert catalog_number_for_identity((9, 8, 7), table, 2) == 'RevisionTwo'
    assert catalog_number_for_identity((9, 8, 7), table, 3) == 'Generic'
    assert catalog_number_for_identity((9, 8, 7), table) == 'Generic'


def test_load_revision_key_and_reject_negative_revision(tmp_path):
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'9:8:7:2': 'RevisionTwo'}))
    assert load_external_catalog(path) == {(9, 8, 7, 2): 'RevisionTwo'}
    path.write_text(json.dumps({'9:8:7:-1': 'Invalid'}))
    with pytest.raises(CatalogError):
        load_external_catalog(path)

def test_revision_override_on_normal_conversion(tmp_path):
    from pathlib import Path
    from xml.etree import ElementTree as ET
    from acd.api import ConvertAcdToL5x
    source = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'1:14:168': 'GenericCPU', '1:14:168:34': 'ConfiguredCPU'}))
    target = tmp_path / 'result.L5X'
    with pytest.warns(RuntimeWarning, match='Incomplete'):
        ConvertAcdToL5x(source, target, catalog_file=str(path)).extract()
    cpu = ET.parse(target).find(".//Module[@Name='Local']")
    assert cpu.get('Major') == '34' and cpu.get('CatalogNumber') == 'ConfiguredCPU'
