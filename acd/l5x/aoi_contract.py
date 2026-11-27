"""Declaration ordering from modern scoped identities, never display names."""
import re
import struct


def ordered_declarations(rows):
    ordered = []
    seen = set()
    scopes = set()
    for oid, raw in rows:
        raw = bytes(raw)
        # Generated editor storage is not an AOI declaration.
        # Names are resolved by the caller before this helper when available.
        if len(raw) < 18:
            raise ValueError('Truncated AOI declaration identity')
        scope, own = struct.unpack_from('<II', raw, 10)
        if own in seen:
            raise ValueError('Ambiguous AOI declaration identity')
        seen.add(own)
        scopes.add(scope)
        ordered.append((own, oid, raw))
    if len(scopes) > 1:
        raise ValueError('Mixed AOI declaration scopes')
    return [(oid, raw) for _, oid, raw in sorted(ordered)]


def execution_word(attributes, key):
    raw = attributes.get(key)
    if raw is None:
        return None
    if len(raw) != 4 or struct.unpack('<I', raw)[0] not in (0, 1):
        raise ValueError('Unsupported AOI execution word')
    return 'true' if struct.unpack('<I', raw)[0] else 'false'
