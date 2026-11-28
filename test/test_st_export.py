"""ST regressions adapted from upstream PR68 plus reference/order coverage.

Only synthetic records and the existing public fixture are used here. Private
Studio reference comparisons are run separately outside the checkout.
"""
import sqlite3
import struct
from types import SimpleNamespace
from xml.dom import minidom
from pathlib import Path

import pytest

from acd.api import ConvertAcdToL5x, ImportProjectFromFile
from acd.l5x.elements import Routine
from acd.l5x.export_l5x import ExportL5x
from acd.l5x import st_decoder
from acd.l5x.st_decoder import STDecoder


def _line(text):
    payload = text.encode("utf-16-le")
    return bytes(24) + b"\xff\xfe\xff" + bytes([len(payload) // 2]) + payload


def _index(ids):
    raw = bytearray(26)
    struct.pack_into("<H", raw, 16, 0x07D2)
    raw[20:24] = b"\xff" * 4
    struct.pack_into("<H", raw, 24, len(ids))
    return bytes(raw) + b"".join(struct.pack("<I", oid) for oid in ids)


def _rx(cip_type, runtime=0, attrs=()):
    raw = bytearray(82)
    struct.pack_into("<H", raw, 10, cip_type)
    struct.pack_into("<H", raw, 0x42, runtime)
    for key, value in attrs:
        raw.extend(struct.pack("<II", key, len(value)) + value)
    struct.pack_into("<II", raw, 74, len(raw) - 74, len(attrs))
    return bytes(raw)


@pytest.fixture
def graph():
    db = sqlite3.connect(":memory:")
    cur = db.cursor()
    cur.execute("CREATE TABLE comps(object_id int,parent_id int,comp_name text,record blob)")
    cur.execute("CREATE TABLE nameless(object_id int,parent_id int,record blob)")
    cur.executemany("INSERT INTO comps VALUES(?,?,?,?)", [
        (1, 0, "Controller", b""), (2, 1, "RxTagCollection", b""),
        (3, 1, "RxMapDeviceCollection", b""), (100, 1, "Routine", b""),
        (30, 2, "__Map:Misleading", _rx(0x6B, 0x1069, [(0x66, struct.pack("<I", 40))])),
        (31, 2, "__Map:Ordinary", _rx(0x6B, 0xC4)),
        (40, 3, "ActualModule", _rx(0x69)),
    ])
    cur.executemany("INSERT INTO nameless VALUES(?,?,?)", [(10, 100, b""), (11, 10, b"")])
    exporter = SimpleNamespace(_cur=cur)

    def decoder(ids, records, layout="complete-counted"):
        cur.execute("INSERT INTO nameless VALUES(12,11,?)", (_index(ids),))
        cur.executemany("INSERT INTO nameless VALUES(?,12,?)", records)
        exporter._id_to_name = dict(cur.execute("SELECT object_id,comp_name FROM comps"))
        from acd.record.attribute_bounds import attribute_layout
        with attribute_layout(layout):
            return STDecoder(exporter)

    yield cur, decoder
    db.close()


def test_index_order_blank_comments_and_distinct_identical_lines(graph):
    _cur, build = graph
    decoder = build([23, 21, 22, 24, 21], [
        (21, _line("x := 1;")), (22, _line("")),
        (23, _line("  (* comment *)\t")), (24, _line("x := 1;")),
    ])
    assert decoder.decode(100) == ["  (* comment *)\t", "x := 1;", "", "x := 1;"]
    assert decoder.all_routines() == [("Routine", 100, decoder.decode(100))]
    assert decoder.decode_numbered(100) == [
        (0, "  (* comment *)\t"), (1, "x := 1;"), (2, ""), (3, "x := 1;")]
    assert decoder.diagnostics[100] == []


def test_module_reference_follows_typed_oid_link_not_name_prefix(graph):
    _cur, build = graph
    decoder = build([21], [(21, _line(
        'GSV(MODULE,@0000001E@,EntryStatus,Status); // __Map:literal @1f@'))])
    assert decoder.decode_numbered(100) == [(
        0, 'GSV(MODULE,ActualModule,EntryStatus,Status); // __Map:literal __Map:Ordinary')]
    assert decoder.diagnostics[100] == []


@pytest.mark.parametrize("invalid", ["missing", "wrong_type", "other_controller", "short_link"])
def test_invalid_module_link_is_reported_without_guessing(graph, invalid):
    cur, build = graph
    if invalid == "missing":
        cur.execute("DELETE FROM comps WHERE object_id=40")
    elif invalid == "wrong_type":
        cur.execute("UPDATE comps SET record=? WHERE object_id=40", (_rx(0x6B),))
    elif invalid == "other_controller":
        cur.execute("UPDATE comps SET parent_id=999 WHERE object_id=3")
    else:
        cur.execute("UPDATE comps SET record=? WHERE object_id=30",
                    (_rx(0x6B, 0x1069, [(0x66, b"\x01")]),))
    decoder = build([21], [(21, _line("GSV(MODULE,@1e@,EntryStatus,Status);"))])
    assert decoder.decode_numbered(100) == [(0, "GSV(MODULE,@1e@,EntryStatus,Status);")]
    assert decoder.diagnostics[100][0]["reference_id"] == 30
    assert decoder.diagnostics[100][0]["line_number"] == 0


def test_unreadable_and_missing_lines_keep_subsequent_source_positions(graph):
    _cur, build = graph
    decoder = build([21, 22, 99, 23], [
        (21, _line("a := 1;")), (22, b"bad"), (23, _line("b := 2;")),
    ])
    assert decoder.decode_numbered(100) == [(0, "a := 1;"), (3, "b := 2;")]
    assert [d["line_number"] for d in decoder.diagnostics[100]] == [1, 2]
    assert [d["record_id"] for d in decoder.diagnostics[100]] == [22, 99]


@pytest.mark.parametrize("record", [
    bytes(24) + b"\xff\xfe\xff\x02x\x00",  # truncated
    bytes(24) + b"\xff\xfe\xff\x01\x00\xdc",  # invalid UTF-16
    _line("a\x01b"),  # cannot be represented in XML 1.0
    _line("abc") + b"\x00\x00",  # unsupported/trailing text, not silently truncated
])
def test_bad_text_is_reported(graph, record):
    _cur, build = graph
    decoder = build([21], [(21, record)])
    assert decoder.decode_numbered(100) == []
    assert len(decoder.diagnostics[100]) == 1


def test_surrogate_pairs_and_long_supported_line_are_not_truncated(graph):
    _cur, build = graph
    text = "\U0001f642" + "x" * 253  # 255 UTF-16 code units
    decoder = build([21], [(21, _line(text))])
    assert decoder.decode_numbered(100) == [(0, text)]
    assert not decoder.diagnostics[100]


def test_missing_routine_index_is_reported(graph):
    _cur, build = graph
    decoder = build([], [])
    assert decoder.decode_numbered(999) is None
    assert decoder.diagnostics[999][0]["reason"] == "no supported ST source index"
    assert decoder.decode_numbered(100) == []  # a counted empty source is distinct
    assert decoder.diagnostics[100] == []


def test_malformed_source_index_and_compiled_siblings_are_not_silently_used(graph):
    cur, build = graph
    compiled = bytearray(_index([25, 26, 27]))
    struct.pack_into("<H", compiled, 16, 0x07D7)
    cur.execute("INSERT INTO nameless VALUES(13,11,?)", (bytes(compiled),))
    cur.executemany("INSERT INTO nameless VALUES(?,13,?)", [
        (25, _line("compiled")), (26, _line("compiled")), (27, _line("compiled"))])
    decoder = build([21], [(21, _line("source"))])
    assert decoder.decode_numbered(100) == [(0, "source")]
    cur.execute("UPDATE nameless SET record=? WHERE object_id=12", (_index([21])[:-1],))
    decoder = STDecoder(SimpleNamespace(_cur=cur, _id_to_name={100: "Routine"}))
    assert decoder.decode_numbered(100) is None
    assert "length/count" in decoder.diagnostics[100][0]["reason"]


def test_serializer_preserves_line_content_numbers_cdata_and_cr():
    text = "\t(* ]]> *)\r x := 1; "
    routine = Routine("r", "r", "ST", [], _st_lines=[(0, ""), (2, text)])
    doc = minidom.parseString(routine.to_xml())
    lines = doc.getElementsByTagName("Line")
    assert [line.getAttribute("Number") for line in lines] == ["0", "2"]
    assert ["".join(n.data for n in line.childNodes) for line in lines] == ["", text]


RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def _st_payloads(path):
    doc = minidom.parse(str(path))
    result = {}
    for routine in doc.getElementsByTagName("Routine"):
        if routine.getAttribute("Type") != "ST":
            continue
        owner = routine.parentNode.parentNode
        key = (owner.tagName, owner.getAttribute("Name"), routine.getAttribute("Name"))
        result[key] = [(line.getAttribute("Number"),
                        "".join(n.data for n in line.childNodes if n.nodeType == n.CDATA_SECTION_NODE))
                       for line in routine.getElementsByTagName("Line")]
    return result


def test_normal_converter_st_matches_public_studio_reference(tmp_path):
    output = tmp_path / "converted.L5X"
    ConvertAcdToL5x(RESOURCES / "ACDTestsWithAOI.ACD", output).extract()
    expected = _st_payloads(RESOURCES / "ACDTestsWithAOI.L5X")
    assert sum(len(lines) for lines in expected.values()) == 6
    assert _st_payloads(output) == expected


def test_unreadable_routine_is_named_in_project_and_saved_xml(monkeypatch, tmp_path):
    def fail(self, routine_id):
        raise ValueError("bad -- source")

    monkeypatch.setattr(STDecoder, "decode_numbered", fail)
    project = ImportProjectFromFile(RESOURCES / "ACDTestsWithAOI.ACD").import_project()
    assert len(project._st_diagnostics) == 1
    assert project._st_diagnostics[0]["routine"] == "STRoutine"
    assert "bad -- source" in project.conversion_notes[0]
    assert "MainProgram/STRoutine" in project.conversion_notes[0]
    assert "conversion notes" in project.to_xml()
    minidom.parseString(project.to_xml())  # comment is well formed despite '--'
    output = tmp_path / "partial.L5X"
    ConvertAcdToL5x(RESOURCES / "ACDTestsWithAOI.ACD", output).extract()
    assert "Incomplete Structured Text" in output.read_text(encoding="utf-8-sig")


def test_unreadable_line_is_reported_with_original_line_number(monkeypatch):
    original = st_decoder._decode_line_text
    failed = []

    def unreadable(record):
        text = original(record)
        if text and not failed:
            failed.append(True)
            raise ValueError("unreadable text")
        return text

    monkeypatch.setattr(st_decoder, "_decode_line_text", unreadable)
    project = ExportL5x(RESOURCES / "ACDTestsWithAOI.ACD").project
    assert len(project._st_diagnostics) == 1
    issue = project._st_diagnostics[0]
    assert issue["routine"] == "STRoutine"
    routine = next(r for p in project.controller.programs for r in p.routines if r.type == "ST")
    assert len(routine._st_lines) == 5
    assert issue["line_number"] not in [n for n, _text in routine._st_lines]

def test_missing_module_link_in_compat_is_never_recovered_by_a_raw_tail_scan(graph):
    cur, build = graph
    # A complete identity with a truncated last attribute cannot establish its target.
    row = cur.execute('SELECT record FROM comps WHERE object_id=30').fetchone()[0]
    cur.execute('UPDATE comps SET record=? WHERE object_id=30', (row[:-2],))
    decoder = build([21], [(21, _line('@0000001E@'))], layout='compat')
    assert decoder.decode_numbered(100) == [(0, '@0000001E@')]
    assert decoder.diagnostics[100][0]['reference_id'] == 30


def test_compat_st_resolves_the_module_back_reference(graph):
    cur, decoder = graph
    module = bytearray(_rx(0x69, 0, [(0x140, struct.pack('<I',30)), (2,b'')]))
    struct.pack_into('<H', module, 8, 166)
    cur.execute('UPDATE comps SET record=? WHERE object_id=40', (module,))
    d = decoder([22], [(22,_line('@0000001e@'))], layout='compat')
    assert d.decode_numbered(100) == [(0, 'ActualModule')]
    assert not d.diagnostics.get(100)
