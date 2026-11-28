"""Run paired, synthetic module links through the public ST and ladder fixture."""
from collections import Counter
import gzip
import json
from pathlib import Path
import struct
from xml.etree import ElementTree as ET

import pytest

from acd.api import ConvertAcdToL5x
from acd.l5x.completeness import IncompleteExportError
from acd.zip.unzip import Unzip
from acd.zip.write_acd import build_acd_bytes


SOURCE = Path(__file__).resolve().parents[1] / "resources" / "ACDTestsWithAOI.ACD"
# The bundled project references its controller DINT in both source languages.
SOURCE_TOKEN = "@bb593e67@"
REF_ID = 0xABCD1234
TAG_COLLECTION = 0xF3E52AD5
MODULE_ID = 0x12FC5769  # ETH_MODULE in the same controller
DISPLAY_NAME = "SyntheticModuleLink"


def make_fixture(path, kind, duplicate):
    archive = Unzip(str(SOURCE))
    files = {}
    with SOURCE.open("rb") as source:
        for record in archive.records:
            source.seek(record.file_offset)
            files[record.filename] = source.read(record.file_length)

    payload = bytearray(82)
    struct.pack_into("<H", payload, 10, 0x6B)
    struct.pack_into("<H", payload, 66, 0x1069)
    for key in (0x66, 0x66 if duplicate else 0x7FFF):
        payload.extend(struct.pack("<III", key, 4, MODULE_ID))
    struct.pack_into("<II", payload, 74, len(payload) - 74, 2)
    header = bytearray(155 if kind == 0xFDFD else 148)
    struct.pack_into("<H", header, 10, 256)
    struct.pack_into("<II", header, 16, REF_ID, TAG_COLLECTION)
    name = DISPLAY_NAME.encode("utf-16-le")
    header[24:24 + len(name)] = name
    if kind == 0xFAFA:
        struct.pack_into("<I", header, 0, len(header) + len(payload))
    body = header + payload
    if kind == 0xFDFD:
        body.extend(bytes(2))

    for filename in ("Comps.Dat", "Nameless.Dat", "SbRegion.Dat"):
        data = files[filename]
        compressed = data.startswith(b"\x1f\x8b")
        data = gzip.decompress(data) if compressed else data
        if filename == "Comps.Dat":
            end = struct.unpack_from("<I", data, 8)[0] + 1
            data = bytearray(data[:end]) + struct.pack("<HI", kind, len(body) + 6) + body
            struct.pack_into("<I", data, 8, len(data) - 1)
            if kind == 0xFAFA:
                count = struct.unpack_from("<I", data, 20)[0]
                struct.pack_into("<I", data, 20, count + 1)
        else:
            token = SOURCE_TOKEN.encode("utf-16-le")
            assert token in data
            data = data.replace(token, f"@{REF_ID:08x}@".encode("utf-16-le"))
        files[filename] = gzip.compress(data) if compressed else data
    path.write_bytes(build_acd_bytes(files, list(files), archive.header._unknown_two))


def occurrences(path, operand):
    locations = Counter()
    controller = ET.parse(path).getroot().find("Controller")
    for scope, selector in (("Program", "Programs/Program"),
                            ("AOI", "AddOnInstructionDefinitions/AddOnInstructionDefinition")):
        for owner in controller.findall(selector):
            for routine in owner.findall("Routines/Routine"):
                for element in routine.findall("STContent/Line") + routine.findall("RLLContent/Rung"):
                    text = element.text if element.tag == "Line" else element.findtext("Text")
                    count = (text or "").count(operand)
                    if count:
                        key = (scope, owner.get("Name"), routine.get("Name"),
                               element.tag, int(element.get("Number")))
                        locations[key] += count
    return locations


@pytest.mark.parametrize("mode", ["compat", "complete-counted"])
@pytest.mark.parametrize("kind", [0xFAFA, 0xFDFD])
def test_duplicate_module_operand_is_diagnosed_at_every_use(tmp_path, mode, kind):
    source = tmp_path / "module.ACD"
    preview = tmp_path / "preview.L5X"
    options = {"rx_attribute_layout": mode}
    make_fixture(source, kind, duplicate=False)
    ConvertAcdToL5x(source, preview, **options).extract()
    expected = occurrences(preview, "ETH_MODULE")
    assert {k[3] for k in expected} == {"Line", "Rung"}
    assert not occurrences(preview, DISPLAY_NAME)

    make_fixture(source, kind, duplicate=True)
    result = ConvertAcdToL5x(source, preview, **options).extract()
    assert not result.complete
    assert not occurrences(preview, DISPLAY_NAME)
    assert occurrences(preview, f"@{REF_ID:08x}@") == expected
    scoped = Counter()
    for issue in result.diagnostics:
        if issue.get("reference_id") != REF_ID:
            continue
        assert "0x66" in issue["reason"]
        if issue["code"] == "st-reference":
            location = ("Line", issue["line_number"])
        else:
            assert issue["code"] == "ladder-reference"
            location = ("Rung", issue["rung_number"])
        scoped[(issue["scope"], issue["owner"], issue["routine"], *location)] += 1
    assert scoped == expected

    strict = tmp_path / "strict.L5X"
    with pytest.raises(IncompleteExportError):
        ConvertAcdToL5x(source, strict, allow_incomplete=False, **options).extract()
    assert not strict.exists()
    sidecar = json.loads(Path(str(strict) + ".diagnostics.json").read_bytes())
    assert not sidecar["complete"]
    assert sidecar == json.loads(Path(str(preview) + ".diagnostics.json").read_bytes())
