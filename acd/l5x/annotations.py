"""Route ordinary tag annotations using source scope and member identities."""
import re
import struct
from collections import defaultdict

from loguru import logger

from acd.record.rx import RxGeneric


MEMBER = re.compile(r'!([0-9a-fA-F]{8})(?=[.\[]|$)')


INVALID_XML = re.compile(r'[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')


def member_map(cur):
    result = {}
    types = cur.execute("SELECT object_id,record FROM comps WHERE record_type!=512 AND parent_id IN "
                        "(SELECT object_id FROM comps WHERE comp_name='RxDataTypeCollection' AND record_type!=512)").fetchall()
    for oid, raw in types:
        children = cur.execute(
            "SELECT m.comp_name,m.record FROM comps m JOIN comps c ON m.parent_id=c.object_id "
            "WHERE c.parent_id=? AND c.comp_name='RxTypeMemberCollection' AND c.record_type!=512 "
            "AND m.record_type!=512 ORDER BY m.seq_number", (oid,),
        ).fetchall()
        if not children:
            continue
        type_record = RxGeneric.from_bytes(bytes(raw))
        for index, (name, record) in enumerate(children):
            member = RxGeneric.from_bytes(bytes(record))
            key = (type_record.comment_id << 16) | struct.unpack_from('<H', member.record_buffer, 2)[0]
            result[key] = None if key in result else (name.upper(), index)
    return result


def operand(ref, mapping):
    if not ref or ref == '.':
        return None

    def resolve(match):
        entry = mapping.get(int(match[1], 16))
        return entry[0] if entry else match[0]

    value = MEMBER.sub(resolve, ref)
    if '!' in value:
        return None
    if value.startswith(('.', '[')):
        return value
    return '[' + value if value[0].isdigit() and ']' in value else '.' + value


def sort_key(ref, resolved, mapping):
    parts = []
    for token in re.findall(r'[^.\[\]]+', ref):
        if re.fullmatch(r'[0-9]+', token):
            parts.append((0, int(token)))
        elif token.startswith('!') and (entry := mapping.get(int(token[1:], 16))):
            parts.append((0, entry[1]))
        else:
            parts.append((1, token))
    category = 1 if resolved.startswith('[') else (0 if resolved[1:].isdigit() else 2)
    return category, parts


def cdata(text):
    return '<![CDATA[' + text.replace('\r\n', '\n').replace(']]>', ']]]]><![CDATA[>').replace('\r', ']]>&#13;<![CDATA[') + ']]>'


def comments_xml(tag):
    from acd.l5x.elements import _escape_xml_attr
    mapping = getattr(tag, '_hex_member_map', {})
    rows = []
    for ref, text in tag._comments:
        target = operand(ref, mapping)
        if target is not None and text:
            rows.append((sort_key(ref, target, mapping), target, text))
    rows.sort(key=lambda row: row[0])
    if not rows:
        return ''
    return '<Comments>' + ''.join(
        '<Comment Operand="' + _escape_xml_attr(target) + '">' + cdata(tag._sanitize_xml_text(text)) + '</Comment>'
        for _, target, text in rows
    ) + '</Comments>'


def materialize(controller, cur):
    mapping = member_map(cur)
    problems = []
    controller._annotation_problems = problems
    by_parent = defaultdict(list)
    claims = defaultdict(list)
    rows = cur.execute(
        "SELECT rowid, seq_number, parent, member_ref, tag_reference, record_string, record_type "
        "FROM comments WHERE rung_content=0 AND record_string!='' AND (parent & 65535) IN (104,107,108) "
        "AND record_type IN (1,2,3,4,5,6,7,8,9,10,11,13,14,15,16,17,19) ORDER BY seq_number,rowid"
    ).fetchall()
    for row in rows:
        by_parent[row[2]].append(row)

    for owner in [controller, *controller.programs]:
        for tag in owner.tags:
            tag._comments = []
            tag._hex_member_map = mapping
            if tag._l5x_exclude:
                continue
            source = cur.execute('SELECT record, parent_id FROM comps WHERE object_id=? AND record_type!=512',
                                 (tag._source_oid,)).fetchone()
            if not source:
                continue
            try:
                record = RxGeneric.from_bytes(bytes(source[0]))
            except (EOFError, ValueError, struct.error) as exc:
                problems.append(('unreadable tag', owner.name, tag.name, tag._source_oid, str(exc)))
                continue
            parent = (record.comment_id << 16) | record.cip_type
            candidates = by_parent.get(parent, ())
            if record.cip_type == 0x68:
                key = struct.unpack_from('<H', record.record_buffer, 2)[0]
                candidates = [r for r in candidates if r[3] in (key, (key << 16) | 0x6b)]
            elif record.cip_type == 0x6c and not candidates:
                # A companion record in the same collection may own these annotations.
                siblings = cur.execute('SELECT record FROM comps WHERE parent_id=? AND comp_name=? '
                                       'AND record_type!=512 AND object_id!=?',
                                       (source[1], tag.name, tag._source_oid)).fetchall()
                parents = []
                for (raw,) in siblings:
                    try:
                        other = RxGeneric.from_bytes(bytes(raw))
                    except (EOFError, ValueError, struct.error):
                        parents.append(None)
                        continue
                    if other.cip_type == 0x6b:
                        parents.append((other.comment_id << 16) | other.cip_type)
                if len(parents) == 1:
                    candidates = by_parent.get(parents[0], ())
                elif len(parents) > 1:
                    problems.append(('ambiguous companion', owner.name, tag.name, tag._source_oid))
            for row in candidates:
                claims[row[0]].append(tag)

    descriptions = defaultdict(list)
    for rowid, seq, parent, member, ref, text, kind in rows:
        targets = claims.get(rowid, ())
        if len(targets) != 1:
            problems.append(('ambiguous owner' if targets else 'unclaimed source', seq, parent, member, ref))
            continue
        tag = targets[0]
        if INVALID_XML.search(text) or INVALID_XML.search(ref):
            problems.append(('invalid XML character', seq, parent, member, ref))
            continue
        if ref in ('', '.'):
            if kind in (1, 2) and (parent & 65535 == 0x68 or member == 0):
                descriptions[tag._source_oid].append((seq, tag, text))
            else:
                problems.append(('missing operand', seq, parent, member, ref))
        elif operand(ref, mapping) is None:
            problems.append(('unresolved member', seq, parent, member, ref))
        else:
            tag._comments.append((ref, text))
    for oid, candidates in descriptions.items():
        texts = {text.replace('\r\n', '\n') for _, _, text in candidates}
        if len(texts) == 1:
            candidates[0][1]._comments.append(('', texts.pop()))
        else:
            problems.append(('conflicting descriptions', oid, tuple(seq for seq, _, _ in candidates)))
    if problems:
        logger.warning("{} tag annotation records or groups remain unresolved; see Controller._annotation_problems",
                       len(problems))
