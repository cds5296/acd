"""Ordered ST source from Nameless.Dat, adapted from hutcheb/acd PRs 58/68.

Follow routine -> descriptor -> aspect -> source index -> line records. Index
order, not database insertion order, determines source order. Unlike the upstream
decoder, require the counted source-index layout and retain unreadable line
positions in diagnostics. Resolve module-reference tags through their 0x66 link
to a module in the same controller; never rewrite names by prefix.

Upstream: https://github.com/hutcheb/acd/pull/58 and /pull/68 (Apache-2.0).
"""
import re
import struct
from collections import defaultdict

from acd.record.attribute_bounds import AmbiguousModuleLink
from acd.record.rx import RxGeneric, read_reference_record


_REFERENCE = re.compile(r"@([0-9a-fA-F]{1,8})@")
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _decode_line_text(record):
    """Read the observed fffeff + u8 code-unit count + UTF-16LE layout.

    Validate the entire payload: an unsupported length encoding must be reported
    rather than quietly returning a truncated line. Empty strings are real lines.
    """
    offset = record.find(b"\xff\xfe\xff")
    if offset < 0 or offset + 4 > len(record):
        raise ValueError("missing ST text header")
    length = record[offset + 3]
    payload = record[offset + 4:]
    if len(payload) != length * 2:
        raise ValueError("ST text length mismatch or unsupported length encoding")
    text = payload.decode("utf-16-le")
    if _ILLEGAL_XML.search(text):
        raise ValueError("ST text contains an XML-illegal character")
    return text


class STDecoder:
    def __init__(self, exporter):
        cur = exporter._cur
        cur.execute("SELECT object_id, parent_id, record FROM nameless ORDER BY rowid")
        self._records = {}
        self._children = defaultdict(list)
        self._duplicates = set()
        for oid, parent, raw in cur.fetchall():
            if oid in self._records:
                self._duplicates.add(oid)
            self._records[oid] = bytes(raw)
            self._children[parent].append(oid)

        cur.execute("SELECT object_id, parent_id, comp_name, record FROM comps")
        self._comps = {oid: (parent, name, bytes(raw))
                       for oid, parent, name, raw in cur.fetchall()}
        self._names = dict(exporter._id_to_name)
        self._bad_references = {}
        self.diagnostics = {}
        self._cache = {}
        self._resolve_module_links()

    def _resolve_module_links(self):
        """Validate typed module references against actual module object IDs.

        A modern RxTag with CIP type 0x1069 is a module reference. Attribute
        0x66 is its target Comps object ID (not text or a display-name suffix).
        Both collections must belong to the same controller. Ordinary tags and
        authored strings remain untouched, even if they contain '__Map:'.
        """
        for oid, (parent, _name, raw) in self._comps.items():
            collection = self._comps.get(parent)
            if not collection or collection[1] != "RxTagCollection":
                continue
            try:
                rx = read_reference_record(raw)
            except AmbiguousModuleLink as exc:
                self._names.pop(oid, None)
                self._bad_references[oid] = str(exc)
                continue
            except Exception:
                continue  # Names of ordinary records do not require Rx decoding.
            if rx.cip_type != 0x6B or rx.main_record.cip_data_type != 0x1069:
                continue
            self._names.pop(oid, None)
            try:
                links = [bytes(a.value) for a in rx.extended_records
                         if a.attribute_id == 0x66]
                if len(links) != 1 or len(links[0]) != 4:
                    raise ValueError("module reference has no unique four-byte 0x66 link")
                target = self._comps.get(struct.unpack("<I", links[0])[0])
                target_collection = self._comps.get(target[0]) if target else None
                if (not target or not target_collection
                        or target_collection[1] != "RxMapDeviceCollection"
                        or target_collection[0] != collection[0]
                        or RxGeneric.from_bytes(target[2]).cip_type != 0x69
                        or not target[1]):
                    raise ValueError("module reference target is not a module in this controller")
                self._names[oid] = target[1]
            except Exception as exc:
                self._bad_references[oid] = str(exc)

    def _index(self, oid):
        raw = self._records[oid]
        # Source index record type 0x07D2, followed by ffffffff, u16 count,
        # then count little-endian u32 line IDs. The upstream byte-22 walk
        # incidentally skipped the count as an unknown ID; read it explicitly.
        if (len(raw) < 26 or struct.unpack_from("<H", raw, 16)[0] != 0x07D2
                or raw[20:24] != b"\xff" * 4):
            return None
        count = struct.unpack_from("<H", raw, 24)[0]
        if len(raw) != 26 + 4 * count:
            raise ValueError("ST source index length/count mismatch")
        return [struct.unpack_from("<I", raw, pos)[0]
                for pos in range(26, len(raw), 4)]

    def _decode_index(self, index, ids):
        lines, issues, seen = [], [], set()
        number = 0
        for oid in ids:
            # Repeated references to one source object are compiler artifacts;
            # distinct records with identical source text remain distinct lines.
            if oid in seen:
                continue
            seen.add(oid)
            try:
                if oid not in self._children.get(index, ()):
                    raise ValueError("ST index references a missing or non-child line record")
                if oid in self._duplicates:
                    raise ValueError("duplicate ST line object ID")
                text = _decode_line_text(self._records[oid])

                def resolve(match):
                    reference = int(match[1], 16)
                    if reference not in self._names:
                        issues.append(dict(line_number=number, record_id=oid,
                                           reference_id=reference,
                                           reason=self._bad_references.get(
                                               reference, "unresolved ST object reference")))
                        return match[0]
                    return self._names[reference]

                lines.append((number, _REFERENCE.sub(resolve, text)))
            except (ValueError, UnicodeError) as exc:
                issues.append(dict(line_number=number, record_id=oid, reason=str(exc)))
            number += 1
        return lines, issues

    def decode_numbered(self, routine_id):
        """Return numbered lines, or None for an unreadable routine.

        diagnostics[routine_id] records omissions and unresolved references.
        A blank source line is (number, ''), not a missing line. Missing lines
        leave gaps in numbering, so subsequent source positions never shift.
        """
        if routine_id in self._cache:
            return self._cache[routine_id]
        candidates, malformed = [], []
        for desc in self._children.get(routine_id, ()):
            for aspect in self._children.get(desc, ()):
                for index in self._children.get(aspect, ()):
                    try:
                        ids = self._index(index)
                    except ValueError as exc:
                        malformed.append(dict(record_id=index, reason=str(exc)))
                        continue
                    if ids is not None:
                        lines, issues = self._decode_index(index, ids)
                        candidates.append((len(ids), lines, issues))
        if not candidates:
            lines = None
            issues = malformed or [dict(reason="no supported ST source index")]
        else:
            # Empty sibling aspects are common. Prefer the aspect with the most
            # source entries, including unreadable entries (not the most successes).
            candidates.sort(key=lambda c: c[0], reverse=True)
            _count, lines, issues = candidates[0]
            issues = list(issues) + malformed
            if any(c[0] == candidates[0][0] and c[1:] != candidates[0][1:]
                   for c in candidates[1:]):
                issues.append(dict(reason="ambiguous ST source aspects"))
        self.diagnostics[routine_id] = issues
        self._cache[routine_id] = lines
        return lines

    def decode(self, routine_id):
        """Compatible upstream API: return ordered strings or None."""
        lines = self.decode_numbered(routine_id)
        return None if lines is None else [text for _number, text in lines]

    def all_routines(self):
        """Compatible discovery API, preserving diagnostics for unreadable data."""
        out = []
        for oid, (_parent, name, _raw) in self._comps.items():
            if oid in self._children:
                lines = self.decode(oid)
                if lines is not None:
                    out.append((name, oid, lines))
        return sorted(out, key=lambda item: item[0])
