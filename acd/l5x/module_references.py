"""Typed module-reference names shared by source languages; no prefix rules."""
import struct
from acd.record.attribute_bounds import AmbiguousModuleLink
from acd.record.rx import RxGeneric, read_reference_record


def resolve(cur, names):
    records = {oid:(parent,name,bytes(raw)) for oid,parent,name,raw in
               cur.execute('SELECT object_id,parent_id,comp_name,record FROM comps')}
    issues = {}
    for oid,(parent,name,raw) in records.items():
        collection = records.get(parent)
        if not collection or collection[1] != 'RxTagCollection':
            continue
        try:
            parsed = read_reference_record(raw)
        except AmbiguousModuleLink as exc:
            names.pop(oid, None)
            issues[oid] = str(exc)
            continue
        except (ValueError,EOFError,struct.error):
            continue
        if parsed.cip_type != 0x6b or parsed.main_record.cip_data_type != 0x1069:
            continue
        links = [bytes(attr.value) for attr in parsed.extended_records if attr.attribute_id == 0x66]
        target=None
        if len(links)==1 and len(links[0])==4:
            target=records.get(struct.unpack('<I',links[0])[0])
        owner=records.get(target[0]) if target else None
        try:
            valid_module = bool(target and RxGeneric.from_bytes(target[2]).cip_type == 0x69)
        except (ValueError, EOFError, struct.error):
            valid_module = False
        if (valid_module and owner and owner[1]=='RxMapDeviceCollection'
                and owner[0]==collection[0] and target[1]
                ):
            names[oid]=target[1]
        else:
            names.pop(oid,None)
            issues[oid]='Unresolved typed module-reference target'
    return issues
