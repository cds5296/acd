import re
import struct
from loguru import logger
from dataclasses import dataclass
from sqlite3 import Cursor
from typing import Optional

from acd.database.dbextract import DatRecord
from acd.generated.comments.fafa_coments import FafaComents


@dataclass
class CommentsRecord:
    _cur: Cursor
    dat_record: DatRecord

    def __post_init__(self):
        entry = CommentsRecord.parse(self.dat_record)
        if entry is not None:
            self._cur.execute("INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", entry)

    @staticmethod
    def _parse_udi_body(body: bytes) -> Optional[tuple]:
        """Parse a UDI (type-12) fafa record body.

        UDI records store metadata like the AOI RevisionNote.  The body layout is:
          [0:8]   8 bytes unknown
          [8:12]  4 bytes some_id
          [12:16] 4 bytes flags
          [16:]   UTF-16LE null-terminated UDI-type string (e.g. "UDI_HISTORY")
                  followed by null padding, then a null-terminated ASCII text string.

        Returns (udi_type, text) or None if the structure is not recognized.
        """
        if len(body) < 20:
            return None
        try:
            # UDI type string starts at offset 16 (after 8 unknown + 4 id + 4 flags).
            utf16_start = 16
            pos = utf16_start
            code_units = []
            while pos + 1 < len(body):
                cu = struct.unpack_from("<H", body, pos)[0]
                if cu == 0:
                    break
                code_units.append(cu)
                pos += 2
            udi_type = "".join(chr(cu) for cu in code_units)
            # Skip null terminator and any subsequent null padding.
            pos += 2
            while pos < len(body) and body[pos] == 0:
                pos += 1
            # Read null-terminated ASCII text.
            text_end = body.find(b"\x00", pos)
            if text_end <= pos:
                return None
            text = body[pos:text_end].decode("utf-8", errors="replace")
            return (udi_type, text)
        except Exception:
            return None

    @staticmethod
    def parse(dat_record: DatRecord) -> Optional[tuple]:
        if dat_record.identifier != 64250:
            return None
        r = None
        try:
            r = FafaComents.from_bytes(dat_record.record.record_buffer)
            # Type-12 (0x0C) records carry UDI metadata such as the AOI RevisionNote.
            # The body is raw bytes; parse it to extract the text.
            if r.header.record_type == 12:
                parsed = CommentsRecord._parse_udi_body(bytes(r.body))
                if parsed is None:
                    return None
                udi_type, text = parsed
                # Only store UDI_HISTORY records (RevisionNote) for now.
                if udi_type != "UDI_HISTORY":
                    return None
                return (
                    r.header.seq_number,
                    r.header.sub_record_length,
                    1,              # object_id placeholder (not used for lookup)
                    text,
                    r.header.record_type,
                    r.header.parent,
                    "__REVISION_NOTE__",
                    0,              # rung_content
                    0,              # member_ref
                )
            if r.header.record_type in (0x03, 0x04, 0x0D, 0x0E):
                tag_ref = r.body.tag_reference.value
                # Tag routing key: unknown_1 bytes [2:4] identifies which tag
                # this comment belongs to within a program (matches the tag's
                # record_buffer[2:4]).  Stored in member_ref for filtering.
                _u1 = bytes(r.body.unknown_1)
                _tag_routing_key = struct.unpack_from("<H", _u1, 2)[0] if len(_u1) >= 4 else 0
            elif r.header.record_type in (5, 6, 7, 8, 9, 10, 11, 15, 16, 17, 19):
                # Types 5-11, 15-17, 19: tag operand comment records.
                # Binary layout (body = raw bytes after the 10-byte header):
                #   [0:8]   8 bytes unknown (zeros)
                #   [8:12]  u32 object_id
                #   [12:14] u16 flags (usually 0x0001)
                #   [14:16] 2 bytes padding
                #   [16:]   UTF-16LE null-terminated tag_reference (operand path)
                #   [after null + padding] ASCII null-terminated record_string
                # Operand path examples by type:
                #   5: [44]     6: [1].0     7: [1].15    8: [156].8
                #   9: [173].14  11: .!hex   15: .!hex[N]  16: .!hex[N]
                #   17: .!hex[N].bit   10/19: metadata keys
                body = bytes(r.body)
                if len(body) < 18:
                    raise ValueError("Short operand annotation")
                obj_id = struct.unpack_from("<I", body, 8)[0]
                _tag_routing_key = struct.unpack_from("<H", body, 2)[0]
                pos = 16
                while pos + 2 <= len(body) and body[pos:pos + 2] != b"\x00\x00":
                    pos += 2
                if pos + 2 > len(body):
                    raise ValueError("Unterminated annotation operand")
                tag_ref = body[16:pos].decode("utf-16-le")
                pos += 2
                while pos < len(body) and body[pos] == 0:
                    pos += 1
                text_end = body.find(b"\x00", pos)
                if pos < len(body) and text_end < 0:
                    raise ValueError("Unterminated annotation text")
                record_string = body[pos:text_end].decode("utf-8")
                return (
                    r.header.seq_number,
                    r.header.sub_record_length,
                    obj_id,
                    record_string,
                    r.header.record_type,
                    r.header.parent,
                    tag_ref,
                    0,                    # rung_content (not applicable)
                    _tag_routing_key,     # member_ref = tag routing key
                )
            else:
                tag_ref = ""
            # For AsciiRecord (type 1 or 2), extract bytes [4:8] of unknown_1.
            # This value is non-zero for rung-level comments and zero for internal
            # metadata strings (FBDRoutineDescription, MainProgramLocalTagDescription, etc.).
            if r.header.record_type in (0x01, 0x02) and len(bytes(r.body.unknown_1)) >= 8:
                rung_content = struct.unpack_from("<I", bytes(r.body.unknown_1), 4)[0]
            else:
                rung_content = 0
            # Extract bytes [0:4] of unknown_1 as member_ref.
            # For type 1/2: low16 = 0x006D for rung comments, high16 = routine key.
            # For type 3/4/13/14: tag routing key from body[2:4] (identifies which
            # tag this comment belongs to within a program).
            if r.header.record_type in (0x01, 0x02) and len(bytes(r.body.unknown_1)) >= 4:
                member_ref = struct.unpack_from("<I", bytes(r.body.unknown_1), 0)[0]
            elif r.header.record_type in (0x03, 0x04, 0x0D, 0x0E):
                member_ref = _tag_routing_key
            else:
                member_ref = 0
            return (
                r.header.seq_number,
                r.header.sub_record_length,
                r.body.object_id,
                r.body.record_string,
                r.header.record_type,
                r.header.parent,
                tag_ref,
                rung_content,
                member_ref,
            )
        except Exception as exc:
            logger.warning("Unreadable comment record (sequence {}): {}",
                           r.header.seq_number if r is not None else None, exc)
            return None

    def replace_tag_references(self, sb_rec):
        m = re.findall("@[A-Za-z0-9]*@", sb_rec)
        for tag in m:
            tag_no = tag[1:-1]
            tag_id = int(tag_no, 16)
            self._cur.execute(
                "SELECT object_id, comp_name FROM comps WHERE object_id=" + str(tag_id)
            )
            results = self._cur.fetchall()
            sb_rec = sb_rec.replace(tag, results[0][1])
        return sb_rec
