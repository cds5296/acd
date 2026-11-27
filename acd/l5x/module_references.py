"""Resolve typed tag/module links within the same controller."""
import struct
from collections import defaultdict

from acd.record.attribute_bounds import AmbiguousModuleLink
from acd.record.rx import read_reference_record


def _links(record, key):
    return [a.value for a in record.extended_records if a.attribute_id == key]


def _one_oid(values):
    if len(values) == 1 and len(values[0]) == 4:
        return int.from_bytes(values[0], "little")
    return None


def resolve(cur, names):
    columns = {row[1] for row in cur.execute("PRAGMA table_info(comps)")}
    query = "SELECT object_id,parent_id,comp_name,record FROM comps"
    if "record_type" in columns:
        query += " WHERE record_type != 512"
    records = {oid: (parent, name, raw) for oid, parent, name, raw in cur.execute(query)}
    tags, modules = {}, {}
    reverse = defaultdict(list)
    issues = {}
    for oid, (parent, name, raw) in records.items():
        collection = records.get(parent)
        if not collection or collection[1] not in ("RxTagCollection", "RxMapDeviceCollection"):
            continue
        try:
            parsed = read_reference_record(raw)
        except AmbiguousModuleLink as exc:
            names.pop(oid, None)
            issues[oid] = str(exc)
            continue
        except (ValueError, EOFError, struct.error):
            continue
        controller, kind, _ = collection
        if kind == "RxTagCollection":
            if parsed.cip_type == 0x6b and parsed.main_record.cip_data_type == 0x1069:
                tags[oid] = (controller, _links(parsed, 0x66))
        elif parsed.cip_type == 0x69 and name:
            back = _links(parsed, 0x140) if parsed.record_format_version == 166 else []
            modules[oid] = (controller, name, back)
            if back and _one_oid(back) is not None:
                reverse[controller, _one_oid(back)].append(oid)

    for oid, (owner, forward) in tags.items():
        candidates = reverse[owner, oid]
        target = _one_oid(forward) if forward else next(iter(candidates), None)
        module = modules.get(target)
        if (module and module[0] == owner and (not candidates or candidates == [target])
                and (not module[2] or _one_oid(module[2]) == oid)):
            names[oid] = module[1]
        else:
            names.pop(oid, None)
            issues[oid] = "Unresolved typed module-reference target"
    return issues
