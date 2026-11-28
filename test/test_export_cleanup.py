from pathlib import Path
import pytest
from acd.api import ImportProjectFromFile, load_acd

SOURCE = Path(__file__).resolve().parents[1] / 'resources' / 'ACDTestsWithAOI.ACD'


def owned_directory(monkeypatch, tmp_path):
    directory = tmp_path / 'owned'
    directory.mkdir()
    monkeypatch.setattr('acd.l5x.export_l5x.tempfile.mkdtemp', lambda **kwargs: str(directory))
    return directory


def test_normal_import_releases_its_directory_and_remains_serializable(monkeypatch, tmp_path):
    directory = owned_directory(monkeypatch, tmp_path)
    project = ImportProjectFromFile(SOURCE).import_project()
    assert not directory.exists()
    assert '<Controller ' in project.to_xml()


def test_failed_archive_releases_owned_database_and_directory(monkeypatch, tmp_path):
    directory = owned_directory(monkeypatch, tmp_path)
    source = tmp_path / 'bad.ACD'
    source.write_bytes(b'invalid archive')
    with pytest.raises((ValueError, RuntimeError)):
        load_acd(source)
    assert not directory.exists()


def test_caller_directory_is_preserved(tmp_path):
    marker = tmp_path / 'keep.txt'
    marker.write_text('caller owned')
    project = load_acd(SOURCE, temp_dir=str(tmp_path))
    assert marker.read_text() == 'caller owned'
    assert (tmp_path / 'acd.db').exists()
    assert '<Controller ' in project.to_xml()


def test_owned_cleanup_does_not_depend_on_cyclic_gc(monkeypatch, tmp_path):
    import gc
    directory = owned_directory(monkeypatch, tmp_path)
    enabled = gc.isenabled()
    gc.disable()
    try:
        project = ImportProjectFromFile(SOURCE).import_project()
        assert not directory.exists()
        assert '<Controller ' in project.to_xml()
    finally:
        if enabled:
            gc.enable()
        gc.collect()

@pytest.mark.parametrize('outcome', ['success', 'bad-archive', 'model-error'])
@pytest.mark.parametrize('existing', [('acd.db', 'Comps.Dat'), ('Comps.Dat',)])
def test_caller_files_survive_collisions(tmp_path, monkeypatch, outcome, existing):
    directory = tmp_path / 'caller'
    directory.mkdir()
    contents = {name: (name.encode() + bytes(range(256))) for name in existing}
    contents['keep.bin'] = b'caller owned'
    for name, data in contents.items():
        (directory / name).write_bytes(data)
    source = SOURCE
    if outcome == 'bad-archive':
        source = tmp_path / 'bad.ACD'
        source.write_bytes(b'invalid archive')
    elif outcome == 'model-error':
        def fail(_):
            raise ValueError('synthetic model failure')
        monkeypatch.setattr('acd.l5x.export_l5x.ProjectBuilder.build', fail)

    if outcome == 'success':
        assert '<Controller ' in load_acd(source, temp_dir=str(directory)).to_xml()
    else:
        with pytest.raises((ValueError, RuntimeError)):
            load_acd(source, temp_dir=str(directory))
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == contents

def test_direct_reader_releases_only_its_child_after_materialization(tmp_path):
    from acd.l5x.export_l5x import ExportL5x

    caller_db = tmp_path / 'acd.db'
    caller_db.write_bytes(b'existing database')
    export = ExportL5x(SOURCE, str(tmp_path))
    try:
        child = Path(export._temp_dir)
        assert child.parent == tmp_path and (child / 'Comps.Dat').exists()
        assert export._cur.execute('SELECT count(*) FROM comps').fetchone()[0] > 0
        project = export.project
        assert not child.exists()
        assert export.project is project and '<Controller ' in project.to_xml()
    finally:
        export.close()
    assert caller_db.read_bytes() == b'existing database'


def test_strict_refusal_preserves_output_and_releases_owned_scratch(tmp_path, monkeypatch):
    from acd.api import ConvertAcdToL5x
    from acd.l5x.completeness import IncompleteExportError

    owned = owned_directory(monkeypatch, tmp_path)
    target = tmp_path / 'existing.L5X'
    target.write_bytes(b'caller output')
    with pytest.raises(IncompleteExportError):
        ConvertAcdToL5x(SOURCE, target, allow_incomplete=False).extract()
    assert target.read_bytes() == b'caller output'
    assert not owned.exists()
