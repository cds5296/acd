from types import SimpleNamespace as N
import pytest
from acd.l5x.completeness import IncompleteExportError, finish


def project():
    return N(controller=N(name='Controller', tags=[], programs=[N(name='Program', tags=[], routines=[N(name='Graph', type='FBD', rungs=[])])], aois=[], modules=[]), conversion_notes=[])


def test_partial_export_returns_false_and_names_missing_body(tmp_path):
    with pytest.warns(RuntimeWarning, match='Incomplete'):
        result = finish(project(), tmp_path / 'output.L5X', True)
    assert not result and not result.complete
    assert any(i['owner'] == 'Program/Graph' and i['code'] == 'routine-body' for i in result.diagnostics)
    assert (tmp_path / 'output.L5X.diagnostics.json').exists()


def test_strict_export_fails_before_output(tmp_path):
    destination = tmp_path / 'output.L5X'
    with pytest.raises(IncompleteExportError):
        finish(project(), destination, False)
    assert not destination.exists()


def test_linked_tag_metadata_is_reported_without_invented_configuration(monkeypatch):
    from unittest.mock import Mock
    from acd.l5x.completeness import source_issues
    cursor = Mock()
    import struct
    raw = bytearray(82)
    struct.pack_into('<I', raw, 74, len(raw)-74)
    cursor.execute.return_value.fetchone.return_value = (raw,)
    parsed = N(cip_type=0x6B, extended_records=[N(attribute_id=0x6B, value=bytes(4)),
                                N(attribute_id=0x66, value=b'\xff'*4)])
    monkeypatch.setattr('acd.record.rx.RxGeneric.from_bytes', lambda raw: parsed)
    controller = N(name="Controller", programs=[], tags=[N(name="Transfer", _source_oid=17, tag_type="Base")])
    issues = source_issues(cursor, controller)
    assert len(issues) == 1
    assert issues[0]['code'] == 'linked-tag-configuration'
    assert issues[0]['object_id'] == 17


def test_bus_gap_is_discovered_before_writing_and_source_codes_are_retained():
    from acd.l5x.completeness import assess
    value = project()
    module = N(name='Module', catalog_number='Known', _ekey_state='Disabled', _connections=[])
    module._build_ports_xml = lambda: setattr(module, '_bus_size_problem', 'Unknown configured size')
    value.controller.modules = [module]
    value._reference_diagnostics = [dict(code='linked-tag-configuration', owner='Controller/Transfer', reason='Unsupported')]
    issues = assess(value)
    assert any(i['code'] == 'module-bus' for i in issues)
    assert any(i['code'] == 'linked-tag-configuration' and i['owner'] == 'Controller/Transfer' for i in issues)


@pytest.mark.parametrize('preview', [True, False])
def test_unreadable_aoi_routine_through_converter(monkeypatch, tmp_path, preview):
    import json
    from pathlib import Path
    import xml.etree.ElementTree as ET
    from acd.api import ConvertAcdToL5x
    from acd.l5x.elements import RoutineBuilder

    fixture = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    build = RoutineBuilder.build
    def fail_logic(builder):
        row = builder._cur.execute('SELECT comp_name FROM comps WHERE object_id=?',
                                   (builder._object_id,)).fetchone()
        if row[0] == 'Logic':
            raise ValueError('synthetic read failure')
        return build(builder)
    monkeypatch.setattr(RoutineBuilder, 'build', fail_logic)
    output = tmp_path / 'result.L5X'
    converter = ConvertAcdToL5x(fixture, output, allow_incomplete=preview)
    if preview:
        with pytest.warns(RuntimeWarning, match='Incomplete'):
            result = converter.extract()
        assert not result
        aoi = ET.parse(output).find(".//AddOnInstructionDefinition[@Name='AddOnInstruction']")
        assert aoi is not None and aoi.find("Routines/Routine[@Name='Logic']") is None
        assert 'INCOMPLETE EXPORT' in output.read_text(encoding='utf-8')
    else:
        with pytest.raises(IncompleteExportError):
            converter.extract()
        assert not output.exists()
    diagnostics = json.loads(Path(str(output) + '.diagnostics.json').read_text())
    issues = [issue for issue in diagnostics['issues'] if issue['code'] == 'unreadable-aoi-routine']
    assert diagnostics['complete'] is False
    assert len(issues) == 1 and issues[0]['owner'] == 'AddOnInstruction/Logic'
    assert issues[0]['object_id'] and issues[0]['reason'] == 'synthetic read failure'


@pytest.mark.parametrize('preview', [False, True])
def test_cli_incomplete_exit_and_sidecar(tmp_path, preview):
    import json
    import subprocess
    import sys
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'
    target = tmp_path / 'new-directory' / 'output.L5X'
    cmd = [sys.executable, '-m', 'acd.l5x.export_l5x', str(fixture), str(target)]
    if preview:
        cmd.append('--allow-incomplete')
    run = subprocess.run(cmd, capture_output=True)
    assert run.returncode == 2, run.stderr
    assert target.exists() == preview
    issues = json.loads(Path(str(target) + '.diagnostics.json').read_text())['issues']
    graph = [i for i in issues if i['code'] == 'routine-body']
    assert any(i['owner'].endswith('/FBDRoutine') for i in graph)
    assert not any(i['owner'].endswith('/STRoutine') for i in issues)
    if preview:
        import xml.etree.ElementTree as ET
        lines = ET.parse(target).findall(".//Routine[@Name='STRoutine']/STContent/Line")
        assert len(lines) == 6
