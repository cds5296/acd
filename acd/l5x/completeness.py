"""Explicit fidelity results for exported content and unsupported configuration."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import struct
import warnings


class IncompleteExportError(ValueError):
    pass


def source_issues(cursor, controller):
    """Inspect only source objects already selected as exported ordinary tags."""
    from acd.record.rx import RxGeneric, LegacyRxGeneric
    issues = []
    for owner in [controller] + list(controller.programs):
        for tag in owner.tags:
            oid = getattr(tag, '_source_oid', None)
            if oid is None or tag.tag_type == 'Alias' or getattr(tag, '_l5x_exclude', False):
                continue
            row = cursor.execute('SELECT record FROM comps WHERE object_id=?', (oid,)).fetchone()
            if row is None:
                continue
            try:
                parsed = RxGeneric.from_bytes(row[0])
            except Exception as exc:
                issues.append(dict(code='unreadable-tag-metadata', object_id=oid,
                                   owner=owner.name + '/' + tag.name, reason=type(exc).__name__))
                continue
            if isinstance(parsed, LegacyRxGeneric) or parsed.cip_type != 0x6B:
                continue
            attrs = {a.attribute_id: bytes(a.value) for a in parsed.extended_records}
            if len(attrs.get(0x6B, b'')) == 4 and len(attrs.get(0x66, b'')) == 4:
                issues.append(dict(code='linked-tag-configuration', object_id=oid,
                                   owner=owner.name + '/' + tag.name,
                                   reason='Modern produced/consumed descriptor is not decoded'))
    return issues


@dataclass(frozen=True)
class ConversionResult:
    output: str
    diagnostics_path: str
    diagnostics: tuple

    @property
    def complete(self):
        return not self.diagnostics

    def __bool__(self):
        return self.complete


def assess(project):
    issues = []
    def add(code, owner, reason):
        issues.append(dict(code=code, owner=owner, reason=str(reason)))
    controller = project.controller
    for note in getattr(project, 'conversion_notes', []):
        add('decoder', 'Project', note)
    for owner in [controller] + list(controller.programs):
        for tag in owner.tags:
            if tag.tag_type != 'Alias' and not getattr(tag, '_l5x_exclude', False):
                problem = getattr(tag, '_value_problem', None)
                if problem or not (getattr(tag, '_value_xml', '') or getattr(tag, '_source_value_xml', '')):
                    add('tag-value', owner.name + '/' + tag.name, problem or 'Source value is not decoded')
    for entry in getattr(controller, '_annotation_problems', []):
        issues.append(dict(code='tag-annotation', owner=controller.name, reason=entry[0], source=entry[1:]))
    for aoi in controller.aois:
        issues.extend(getattr(aoi, '_unreadable_routines', []))
        issues.extend(getattr(aoi, '_declaration_issues', []))
        add('aoi-configuration', aoi.name, 'AOI class, postscan and enable-in-false settings are not decoded')
    for owner in list(controller.programs) + list(controller.aois):
        for routine in owner.routines:
            if routine.type != 'RLL':
                add('routine-body', owner.name + '/' + routine.name, 'Unsupported executable routine type ' + routine.type)
            for text in routine.rungs:
                if re.search(r'@[0-9A-Fa-f]+@', text or ''):
                    add('ladder-reference', owner.name + '/' + routine.name, 'Unresolved object reference')
    for module in controller.modules:
        if getattr(module, 'inhibited', False) is None:
            add('module-inhibition', module.name, 'Unsupported module inhibition layout')
        if hasattr(module, '_build_ports_xml'):
            module._build_ports_xml()  # Discover missing configured sizes before writing.
        if module.catalog_number.startswith('CIP-') or not module.catalog_number:
            add('catalog', module.name, 'Module catalog identity is unresolved')
        if not getattr(module, '_ekey_state', None):
            add('keying', module.name, 'Module electronic keying is unresolved')
        if getattr(module, '_bus_size_problem', None):
            add('module-bus', module.name, module._bus_size_problem)
        if getattr(module, '_connections', []):
            add('module-configuration', module.name, 'Connection and I/O configuration are not fully decoded')
    for reason in getattr(controller, '_skipped_tags', []):
        add('skipped-tag', controller.name, reason)
    for reason in getattr(controller, '_safety_map_issues', []):
        add('safety-reference', controller.name, reason)
    for reason in getattr(controller, '_metadata_problems', []):
        add('controller-metadata', controller.name, reason)
    for reason in getattr(project, '_reference_diagnostics', []):
        if isinstance(reason, dict):
            record = dict(reason)
            record.setdefault('code', 'reference')
            record.setdefault('owner', controller.name)
            issues.append(record)
        else:
            add('reference', controller.name, reason)
    # These sections currently have schema placeholders, not verified source decoders.
    add('controller-configuration', controller.name,
        'Clock, security, redundancy, synchronization and safety configuration are not fully decoded')
    return tuple(issues)


def finish(project, destination, allow_incomplete):
    issues = assess(project)
    path = str(destination) + '.diagnostics.json'
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(dict(complete=not issues, issues=issues), indent=2), encoding='utf-8')
    if issues and not allow_incomplete:
        raise IncompleteExportError('Conversion is incomplete; see ' + path)
    if issues:
        warnings.warn('Incomplete L5X export: ' + str(len(issues)) + ' diagnostics; see ' + path, RuntimeWarning, stacklevel=2)
    return ConversionResult(str(destination), path, issues)
