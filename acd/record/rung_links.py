"""Read the fixed-width RegnLink rows used for rung comment identities."""
import struct

from loguru import logger


def supplement_rung_keys(path, cursor, keys):
    """Use a scoped link only where the AST supplied no identity for that object."""
    if not path.is_file():
        return
    raw = path.read_bytes()
    if len(raw) < 52 or raw[40:52] != bytes.fromhex('81000000 1600ffff 12001000'):
        logger.warning("Unrecognized RegnLink layout; skipping supplemental rung keys")
        return
    end, pointer = struct.unpack_from('<II', raw, 8)
    end += 1  # The header stores the last used byte, before page padding.
    count = struct.unpack_from('<I', raw, 24)[0]
    if end > len(raw) or pointer + 22 > end or raw[pointer:pointer + 2] != b'\xfe\xfe':
        logger.warning("Invalid RegnLink pointer region")
        return
    region = struct.unpack_from('<I', raw, pointer + 18)[0]
    if region + 18 > end or raw[region:region + 2] != b'\xfe\xfe':
        logger.warning("Invalid RegnLink record region")
        return
    size = struct.unpack_from('<I', raw, region + 2)[0]
    start = region + size
    if size < 18 or start + count * 22 != end:
        logger.warning("Invalid RegnLink record extent")
        return

    owners = dict(cursor.execute(
        "SELECT rm.object_id, MIN(rm.parent_id) FROM region_map rm "
        "JOIN rungs ON rungs.object_id=rm.object_id GROUP BY rm.object_id "
        "HAVING MIN(rm.parent_id)=MAX(rm.parent_id)"
    ))
    links = {}
    for flag, identity, owner, oid, _ in struct.iter_unpack('<6sIIII', memoryview(raw)[start:end]):
        if flag != b'\x00\x00\x02\x00\x00\x01' or owners.get(oid) != owner or oid in keys:
            continue
        key = identity & 0xffff
        links[oid] = None if oid in links and links[oid] != key else key
    keys.update(links)
