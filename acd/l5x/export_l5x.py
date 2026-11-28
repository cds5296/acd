import argparse
import os
import shutil
import sqlite3
import struct
import tempfile
from contextlib import ExitStack
from weakref import finalize
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Cursor
from typing import Dict, List, Tuple, Union

from acd.database.dbextract import DbExtract
from acd.zip.unzip import Unzip
from loguru import logger as log

from acd.l5x.catalog_numbers import load_external_catalog
from acd.l5x.elements import (
    Controller,
    ControllerBuilder,
    ProjectBuilder,
    RSLogix5000Content,
)
from acd.record.comments import CommentsRecord
from acd.record.comps import CompsRecord
from acd.record.nameless import NamelessRecord
from acd.record.sbregion import SbRegionRecord
from acd.record.rung_links import supplement_rung_keys


@dataclass
class ExportL5x:
    input_filename: os.PathLike
    # An explicit directory retains extraction files for inspection when vacant.
    _temp_dir: Union[str, None] = None
    _controller: Union[Controller, None] = None
    _project: Union[RSLogix5000Content, None] = None
    # Optional richer catalog (CIP identity -> catalog number) used to resolve
    # module CatalogNumbers and the controller ProcessorType. None (the default)
    # means use the built-in CATALOG_NUMBERS only. A merged table (built-in with
    # an external catalog on top, via catalog_numbers.merge_catalog) lets an
    # out-of-table CPU resolve to a real part number so the L5X imports cleanly
    # in Studio 5000 (the A3 finding). See acd/l5x/catalog_numbers.py.
    _catalog_table: Union[Dict[Tuple[int, int, int], str], None] = None

    def __post_init__(self):
        self._resources = ExitStack()
        # A direct reader may use its cursor without ever requesting .project.
        self._release = finalize(self, self._resources.close)
        try:
            if self._temp_dir is None:
                self._own_workspace()
            archive = Unzip(self.input_filename)
            self._prepare_directory(archive)
            self._initialize(archive)
        except BaseException:
            if getattr(self, '_db', None) is not None:
                self._db.close()
            self.close()
            raise
        self._resources.callback(self._db.close)

    def _own_workspace(self, parent=None):
        self._temp_dir = tempfile.mkdtemp(prefix="acd-build-", dir=parent)
        self._resources.callback(shutil.rmtree, self._temp_dir)

    def _prepare_directory(self, archive):
        directory = Path(self._temp_dir)
        directory.mkdir(parents=True, exist_ok=True)
        db_name = "acd.db"
        sqlite_files = {db_name, "acd.db-wal", "acd.db-shm", "acd.db-journal"}
        names = set()
        for entry in archive.records:
            name = entry.filename
            if (not name or Path(name).name != name or name in (".", "..")
                    or name.casefold() in sqlite_files or name.casefold() in names):
                raise ValueError("Invalid or conflicting extraction filename")
            names.add(name.casefold())

        # Reserve the database exclusively. Existing archive members or SQLite
        # sidecars belong to the caller, even if they look like previous exports.
        destinations = [entry.filename for entry in archive.records]
        destinations.extend(sqlite_files - {db_name})
        if not any(os.path.lexists(directory / name) for name in destinations):
            try:
                (directory / db_name).touch(exist_ok=False)
                return
            except FileExistsError:
                pass
        self._own_workspace(directory)

    def close(self):
        """Release the database first, then remove only owned scratch."""
        self._release()

    def _initialize(self, archive):
        _DEFAULT_SQL_DATABASE_NAME = "acd.db"
        self._db = sqlite3.connect(
            os.path.join(self._temp_dir, _DEFAULT_SQL_DATABASE_NAME)
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=OFF")
        self._cur: Cursor = self._db.cursor()

        log.debug("Create Comps table in sqllite db")
        self._cur.execute(
            "CREATE TABLE comps(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB NOT NULL)"
        )
        log.debug("Create pointers table in sqllite db")
        self._cur.execute(
            "CREATE TABLE pointers(object_id int, parent_id int, comp_name text, seq_number int, record_type int, record BLOB NOT NULL)"
        )
        log.debug("Create Rungs table in sqllite db")
        self._cur.execute(
            "CREATE TABLE rungs(object_id int, rung text, seq_number int)"
        )
        log.debug("Create Region_map table in sqllite db")
        self._cur.execute(
            "CREATE TABLE region_map(object_id int, parent_id int, unknown int, seq_no int, record BLOB NOT NULL)"
        )
        log.debug("Create Comments table in sqllite db")
        self._cur.execute(
            "CREATE TABLE comments(seq_number int, sub_record_length int, object_id int, record_string text, record_type int, parent int, tag_reference text, rung_content int, member_ref int)"
        )

        log.debug("Create Nameless table in sqllite db")
        self._cur.execute(
            "CREATE TABLE nameless(object_id int, parent_id int, record BLOB NOT NULL)"
        )

        log.info("Extracting ACD database file")
        archive.write_files(self._temp_dir, overwrite=False)

        # Preserve all embedded files in original order for round-trip writing.
        # Read directly from the ACD archive (pre-decompression) so that
        # compressed files are carried as-is and write-back is byte-identical.
        self._file_order: List[str] = [r.filename for r in archive.records]
        self._footer_unknown: int = archive.header._unknown_two
        self._raw_files: Dict[str, bytes] = {}
        with open(self.input_filename, "rb") as acd_fh:
            for record in archive.records:
                acd_fh.seek(record.file_offset)
                self._raw_files[record.filename] = acd_fh.read(record.file_length)

        log.info("Getting records from ACD Comps file and storing in sqllite database")
        comps_db = DbExtract(os.path.join(self._temp_dir, "Comps.Dat")).read()
        # Deduplicate by object_id. When duplicate object_ids exist (e.g. a routine that
        # appears twice in Comps.Dat with different record_type values), keep the entry
        # with the largest record because the smaller/later entry is typically a truncated
        # or partial record (e.g. record_type=271 vs 259 for routines) that fails to parse
        # correctly with RxGeneric. The full record is always the largest one.
        comps_by_id = {}
        for record in comps_db.records.record:
            t = CompsRecord.parse(record)
            if t is not None:
                oid = t[0]
                if oid not in comps_by_id or len(t[5]) > len(comps_by_id[oid][5]):
                    comps_by_id[oid] = t
        self._cur.executemany("INSERT INTO comps VALUES (?,?,?,?,?,?)", comps_by_id.values())
        self._db.commit()

        # Build name lookup for SbRegion tag reference resolution (object_id → comp_name).
        # Store on self for use during write-back (patch_sbregion_dat needs id_to_name).
        name_lookup = {oid: t[2] for oid, t in comps_by_id.items()}
        self._id_to_name: Dict[int, str] = name_lookup
        from acd.l5x.module_references import resolve
        self._module_reference_issues = resolve(self._cur, name_lookup)

        # SbRegion (rungs) is read BEFORE the Region Map. populate_region_map's
        # fallback path (see _parse_region_map) needs the set of real rung
        # object_ids already available to empirically re-align a Region Map
        # record whose stored length doesn't match its on-disk size -- a
        # fixed-offset guess isn't reliable enough (see that method's
        # docstring-length comment for what went wrong when it was).
        log.info(
            "Getting records from ACD SbRegion file and storing in sqllite database"
        )
        sb_region_db = DbExtract(os.path.join(self._temp_dir, "SbRegion.Dat")).read()
        self._rung_keys = {}
        rung_tuples = [t for record in sb_region_db.records.record if (t := SbRegionRecord.parse(record, name_lookup, self._rung_keys)) is not None]
        self._cur.executemany("INSERT INTO rungs VALUES (?,?,?)", rung_tuples)
        self._db.commit()

        log.info(
            "Getting records from ACD Region Map file and storing in sqllite database"
        )
        self.populate_region_map()

        # Now that `rungs` (and its on-disk insertion order) and `region_map`
        # both exist, try to recover any Region Map entry that was truncated
        # before its object_id (see populate_region_map/_parse_region_map).
        self._repair_region_map_gaps()

        log.info(
            "Getting records from ACD Comments file and storing in sqllite database"
        )
        comments_db = DbExtract(os.path.join(self._temp_dir, "Comments.Dat")).read()
        comment_tuples = [t for record in comments_db.records.record if (t := CommentsRecord.parse(record)) is not None]
        self._cur.executemany("INSERT INTO comments VALUES (?,?,?,?,?,?,?,?,?)", comment_tuples)
        self._db.commit()

        log.info(
            "Getting records from ACD Nameless file and storing in sqllite database"
        )
        nameless_db = DbExtract(os.path.join(self._temp_dir, "Nameless.Dat")).read()
        nameless_tuples = [t for record in nameless_db.records.record if (t := NamelessRecord.parse(record)) is not None]
        self._cur.executemany("INSERT INTO nameless VALUES (?,?,?)", nameless_tuples)
        self._db.commit()

        log.info("Creating indexes for fast object graph queries")
        self._cur.execute("CREATE INDEX idx_comps_object_id ON comps(object_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_id ON comps(parent_id)")
        self._cur.execute("CREATE INDEX idx_comps_parent_name ON comps(parent_id, comp_name)")
        self._cur.execute("CREATE INDEX idx_rungs_object_id ON rungs(object_id)")
        self._cur.execute("CREATE INDEX idx_region_map_parent_id ON region_map(parent_id)")
        self._cur.execute("CREATE INDEX idx_comments_parent ON comments(parent)")
        self._db.commit()

        supplement_rung_keys(Path(self._temp_dir) / "RegnLink.Dat", self._cur, self._rung_keys)

    @property
    def controller(self):
        if self._controller is None:
            self._controller = ControllerBuilder(
                self._cur, _catalog_table=self._catalog_table, _rung_keys=self._rung_keys
            ).build()
            from acd.l5x.numeric_values import materialize
            from acd.l5x.annotations import materialize as materialize_annotations
            materialize(self._controller, self._cur)
            materialize_annotations(self._controller, self._cur)
            from acd.l5x.values import attach_values, load_layouts
            self._layout_problems = []
            layouts = load_layouts(Path(self._temp_dir) / 'TagInfo.XML', self._layout_problems)
            attach_values(self._controller, self._cur, layouts, self._layout_problems)
            from acd.l5x.safety_map import materialize as materialize_map
            materialize_map(self._controller, self._cur)
            self._populate_st()
        return self._controller

    def _populate_st(self):
        self._st_diagnostics = []
        self._st_notes = []
        owners = [("Program", p) for p in self._controller.programs]
        owners += [("AOI", a) for a in self._controller.aois]
        routines = [(kind, owner.name, routine)
                    for kind, owner in owners for routine in owner.routines
                    if routine.type == "ST"]
        if not routines:
            return
        decoder, initialization_error = None, None
        try:
            decoder = self.st_decoder()
        except Exception as exc:
            initialization_error = str(exc)
        for kind, owner, routine in routines:
            try:
                if decoder is None:
                    raise ValueError(initialization_error)
                lines = decoder.decode_numbered(routine._object_id)
                routine._st_lines = lines if lines is not None else []
                issues = decoder.diagnostics.get(routine._object_id, [])
            except Exception as exc:
                issues = [dict(reason="ST routine could not be decoded: " + str(exc))]
            for issue in issues:
                record = dict(scope=kind, owner=owner, routine=routine.name,
                              routine_id=routine._object_id, **issue)
                self._st_diagnostics.append(record)
                line = issue.get("line_number")
                position = f" line {line}" if line is not None else ""
                note = (f"Incomplete Structured Text {kind} {owner}/{routine.name}"
                        f"{position}: {issue['reason']}")
                self._st_notes.append(note)
                log.warning(note)

    def st_decoder(self) -> "STDecoder": # noqa
        """Return an STDecoder for this ACD file.

        Must be called **before** accessing ``.project`` (which closes the
        sqlite connection).  The returned :class:`STDecoder` holds its own
        in-memory copy of all necessary data and remains usable after
        ``.project`` is called.

        Example::

            exp = ExportL5x("project.acd")
            dec = exp.st_decoder()          # create while DB is open
            project = exp.project           # DB closes here; dec still works

            for name, comps_id, lines in dec.all_routines():
                print(f"{name}: {len(lines)} lines")
        """
        from acd.l5x.st_decoder import STDecoder  # local import avoids circular refs
        return STDecoder(self)

    @property
    def project(self):
        if self._project is None:
            try:
                self._project = self._build_project()
            finally:
                self.close()
        return self._project

    def _build_project(self):
        project = ProjectBuilder(
            Path(os.path.join(self._temp_dir, "QuickInfo.XML"))
        ).build()
        project.controller = self.controller
        project.conversion_notes = list(self._st_notes)
        project._st_diagnostics = list(self._st_diagnostics)
        project._raw_files = self._raw_files
        project._file_order = self._file_order
        project._footer_unknown = self._footer_unknown
        project._id_to_name = self._id_to_name
        project._reference_diagnostics = [dict(code='module-reference', object_id=oid, reason=reason)
                                                for oid, reason in self._module_reference_issues.items()]
        project._reference_diagnostics.extend(self._layout_problems)
        from acd.l5x.completeness import source_issues
        project._reference_diagnostics.extend(source_issues(self._cur, project.controller))
        return project

    def populate_region_map(self):
        self._cur.execute(
            "SELECT comp_name, object_id, parent_id, record FROM comps WHERE parent_id=0 AND comp_name='Region Map'"
        )
        results = self._cur.fetchall()

        # Entries with a resolved object_id are inserted immediately. A
        # trailing 12-byte (parent_id, unknown, seq) triple with no object_id
        # (see _parse_region_map) is stashed for _repair_region_map_gaps(),
        # which can only run once the `rungs` table exists.
        self._pending_region_map_gaps: List[Tuple[int, int, int]] = []

        if len(results) == 0:
            return
        record = results[0][3]

        self._cur.execute("SELECT object_id FROM rungs")
        known_rung_ids = {row[0] for row in self._cur.fetchall()}

        complete_entries = []
        for object_id, parent_id, unknown, seq, raw in self._parse_region_map(record, known_rung_ids):
            if object_id is None:
                self._pending_region_map_gaps.append((parent_id, unknown, seq))
            else:
                complete_entries.append((object_id, parent_id, unknown, seq, raw))

        self._cur.executemany(
            "INSERT INTO region_map VALUES (?, ?, ?, ?, ?)",
            complete_entries,
        )
        self._db.commit()

    def _repair_region_map_gaps(self):
        """Resolve region_map entries whose object_id was truncated from the
        on-disk Region Map record.

        Some ACD files store a final region_map entry as a short 12-byte
        (parent_id, unknown, seq) triple instead of the usual 16 bytes,
        omitting the object_id entirely (observed for a routine's last rung
        after certain edits in Studio 5000; reproduced on a real project
        file). Without recovering it, that one rung silently vanishes from
        the exported routine -- with no error, which is the worst failure
        mode for a ladder-logic export.

        The `rungs` table preserves rungs in their original on-disk order
        (SbRegion.Dat is read and inserted sequentially, and SQLite's
        implicit `rowid` tracks that insertion order). For the routine
        (parent_id) with the gap, every *other* rung of that routine already
        has a resolved object_id and a known seq. If the gap's seq falls
        between two already-resolved neighbours, the missing rung must be
        whichever one sits between those neighbours in on-disk order -- so
        if exactly one unclaimed rung qualifies, it's the answer.
        """
        if not getattr(self, "_pending_region_map_gaps", None):
            return

        # object_id -> rowid, for every rung, in on-disk insertion order.
        self._cur.execute("SELECT rowid, object_id FROM rungs ORDER BY rowid")
        rung_rowid_by_object_id = {object_id: rowid for rowid, object_id in self._cur.fetchall()}

        # object_ids already claimed by *any* routine's region_map, so a
        # candidate can't be assigned to two places.
        self._cur.execute("SELECT object_id FROM region_map")
        claimed = {row[0] for row in self._cur.fetchall()}

        resolved = []
        unresolved = []
        for parent_id, unknown, seq in self._pending_region_map_gaps:
            self._cur.execute(
                "SELECT object_id, seq_no FROM region_map WHERE parent_id=? ORDER BY seq_no",
                (parent_id,),
            )
            siblings = self._cur.fetchall()
            prev_obj = next((oid for oid, s in siblings if s == seq - 1), None)
            next_obj = next((oid for oid, s in siblings if s == seq + 1), None)
            prev_row = rung_rowid_by_object_id.get(prev_obj)
            next_row = rung_rowid_by_object_id.get(next_obj)

            candidates = [
                object_id
                for object_id, rowid in rung_rowid_by_object_id.items()
                if object_id not in claimed
                and (prev_row is None or rowid > prev_row)
                and (next_row is None or rowid < next_row)
            ]

            if len(candidates) == 1:
                object_id = candidates[0]
                claimed.add(object_id)
                resolved.append((object_id, parent_id, unknown, seq, b""))
                log.info(
                    f"Recovered a truncated Region Map entry: routine {parent_id}, "
                    f"seq {seq} -> rung object_id {object_id} (resolved by on-disk order)"
                )
            else:
                unresolved.append((parent_id, unknown, seq, len(candidates)))

        if resolved:
            self._cur.executemany("INSERT INTO region_map VALUES (?, ?, ?, ?, ?)", resolved)
            self._db.commit()
        for parent_id, unknown, seq, n_candidates in unresolved:
            log.warning(
                f"Could not recover truncated Region Map entry for routine {parent_id}, "
                f"seq {seq} ({n_candidates} ambiguous candidates) -- that rung will be "
                "missing from the export"
            )

    @staticmethod
    def _find_region_map_alignment(record, known_rung_ids):
        """Empirically find where 16-byte Region Map entries actually start.

        Used only as a last resort (see the caller) when neither the modern
        nor the legacy self-described length matches the record's real size,
        so the identifier_offset can't be read off a length field. A first
        attempt at this fallback just assumed entries still start at the
        modern format's fixed 0x4E offset -- wrong on a real project file:
        that file's true entries started at byte 119 (confirmed by scanning
        for known object_ids), which isn't even 16-byte-congruent with 0x4E
        (119 - 0x4E = 41, not a multiple of 16). Trusting that wrong offset
        silently produced garbage (parent_id, object_id) pairs that mostly
        didn't match anything real, so entire routines' rungs were dropped
        with nothing louder than the generic length-mismatch warning.

        Instead, brute-force every candidate byte offset in the record,
        interpret it as a 16-byte-strided array of (parent_id, unknown, seq,
        object_id) entries, and score it by how many of its object_id fields
        are real rung object_ids (`known_rung_ids`, the already-populated
        `rungs` table -- see the caller in populate_region_map). The correct
        offset stands out sharply: on the file that motivated this, the
        right offset matched 813/827 entries (98%) while every other offset
        matched 0.

        Returns the best offset, or None if nothing scored well enough to
        trust (caller then falls back to the old fixed-offset guess rather
        than fabricating an offset with no support).
        """
        if not known_rung_ids:
            return None
        best_offset = None
        best_ratio = 0.0
        best_hits = 0
        search_limit = min(len(record) - 16, 512)
        for offset in range(0, max(search_limit, 0)):
            hits = 0
            total = 0
            pos = offset
            while pos + 16 <= len(record):
                object_id = struct.unpack_from("<I", record, pos + 12)[0]
                total += 1
                if object_id in known_rung_ids:
                    hits += 1
                pos += 16
            if total < 3:
                continue
            ratio = hits / total
            if ratio > best_ratio or (ratio == best_ratio and hits > best_hits):
                best_offset, best_ratio, best_hits = offset, ratio, hits
        # Require strong, unambiguous support before trusting it: a real
        # alignment matches the overwhelming majority of entries (the file
        # this was reverse-engineered against hit 98%), not a coincidence.
        if best_offset is not None and best_ratio >= 0.8 and best_hits >= 3:
            return best_offset
        return None

    @staticmethod
    def _parse_region_map(record, known_rung_ids=None):
        # Current empty projects use a header-only Region Map without a length
        # field or entries.
        if len(record) == 0x4A:
            return []

        # Legacy records have a 28-byte prefix and count bytes after that
        # prefix. Modern records have a 78-byte prefix and include four bytes
        # preceding the entries in their stored length.
        if (
            len(record) >= 78
            and struct.unpack_from("<I", record, 0x4A)[0]
            == len(record) - 0x4A
        ):
            identifier_offset = 0x4E
            entries_length = struct.unpack_from("<I", record, 0x4A)[0] - 4
            # Current records can carry a 12-byte non-entry trailer.
            allowed_trailer_lengths = (0, 12)
        else:
            legacy_length = (
                struct.unpack_from("<I", record, 0x18)[0]
                if len(record) >= 28
                else 0
            )
            if legacy_length in (len(record) - 0x1C, len(record) - 0x18):
                identifier_offset = 0x1C
                entries_length = len(record) - identifier_offset
                allowed_trailer_lengths = (0,)
            elif len(record) >= 78:
                # Neither the modern nor the legacy self-described length
                # field matches the record's actual on-disk size -- observed
                # on real project files where the stored 0x4A length is
                # stale (entries were appended without the header's own
                # length prefix being rewritten to match). Try to find where
                # entries really start by empirical alignment against known
                # rung object_ids (see _find_region_map_alignment); only
                # fall back to the historical fixed-0x4E guess if that
                # search finds nothing trustworthy, since a wrong guess here
                # silently drops real logic rather than erroring.
                found_offset = ExportL5x._find_region_map_alignment(record, known_rung_ids)
                if found_offset is not None:
                    identifier_offset = found_offset
                    log.warning(
                        "Region Map record's stored length (0x4A) doesn't match "
                        f"its on-disk size ({len(record)} bytes) -- found real "
                        f"entries starting at offset {found_offset} (not the "
                        "usual 0x4E) by matching against known rung object_ids"
                    )
                else:
                    identifier_offset = 0x4E
                    log.warning(
                        "Region Map record's stored length (0x4A) doesn't match "
                        f"its on-disk size ({len(record)} bytes), and no better "
                        "alignment could be confirmed against known rung "
                        "object_ids -- falling back to the modern 0x4E offset, "
                        "which may not be correct for this record"
                    )
                entries_length = len(record) - identifier_offset
                allowed_trailer_lengths = range(16)
            else:
                raise ValueError("Invalid Region Map length")

        trailer_length = entries_length % 16
        if trailer_length not in allowed_trailer_lengths:
            raise ValueError("Invalid Region Map entry length")
        record_end = identifier_offset + entries_length - trailer_length
        entries = []
        while identifier_offset + 16 <= record_end:
            (
                parent_id_identifier,
                unknown_identifier,
                seq_identifier,
                object_id_identifier,
            ) = struct.unpack_from(
                "<IIII", record, identifier_offset
            )
            entries.append(
                (
                    object_id_identifier,
                    parent_id_identifier,
                    unknown_identifier,
                    seq_identifier,
                    record[identifier_offset : identifier_offset + 16],
                )
            )
            identifier_offset += 16

        # A 12-byte trailer is normally an inert footer. But some records
        # (observed on a real project after certain edits) instead store a
        # final entry truncated to 12 bytes: (parent_id, unknown, seq) with
        # the object_id field simply missing. Distinguish the two cases by
        # checking whether the trailer's first field matches a parent_id we
        # already saw as a real entry above -- an inert footer has no reason
        # to coincidentally embed a real routine's object_id there. When it
        # does, surface it as an entry with object_id=None so the caller can
        # attempt to recover the missing id from other evidence (see
        # ExportL5x._repair_region_map_gaps) instead of the rung silently
        # disappearing from the export.
        if trailer_length == 12 and record_end + 12 <= len(record):
            parent_id_identifier, unknown_identifier, seq_identifier = struct.unpack_from(
                "<III", record, record_end
            )
            known_parent_ids = {e[1] for e in entries}
            if parent_id_identifier in known_parent_ids:
                entries.append(
                    (
                        None,
                        parent_id_identifier,
                        unknown_identifier,
                        seq_identifier,
                        record[record_end : record_end + 12],
                    )
                )

        return entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read an ACD file and export the database as an L5X file"
    )
    parser.add_argument(
        "input", metavar="input", type=str, nargs="+", help="The file to be converted"
    )
    parser.add_argument(
        "output",
        metavar="output",
        type=str,
        nargs="+",
        help="Filename of the exported file",
    )
    parser.add_argument(
        "--catalog",
        metavar="JSON",
        type=str,
        default=None,
        help="External catalog JSON path (merges over built-in CATALOG_NUMBERS)",
    )

    parser.add_argument('--allow-incomplete', action='store_true',
                        help='Write a diagnostic preview despite unresolved content')
    parser.add_argument('--rx-attribute-layout', choices=('compat', 'complete-counted'),
                        default='compat', help='Explicit input contract; never autodetected')
    args = parser.parse_args()
    from acd.api import ConvertAcdToL5x
    from acd.l5x.completeness import IncompleteExportError
    import sys
    try:
        result = ConvertAcdToL5x(
            args.input[0], args.output[0], catalog_file=args.catalog,
            rx_attribute_layout=args.rx_attribute_layout, allow_incomplete=args.allow_incomplete,
        ).extract()
    except IncompleteExportError as exc:
        log.error(str(exc))
        sys.exit(2)
    if not result.complete:
        log.error('Wrote an incomplete preview; see diagnostics sidecar')
        sys.exit(2)
