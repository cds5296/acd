"""Source-backed DTI/TagInfo value decoding.

Uses DTI attribute 0x66 and TagInfo offsets with Logix string /
REAL encoding. Missing bytes, layouts and unsupported types never become zeros.
"""
from dataclasses import dataclass
import itertools
import math
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from loguru import logger as log
from acd.l5x.numeric_values import dimensions as parse_dimensions, source_value

PRIMITIVES = {'BOOL': ('B', 1), 'SINT': ('b', 1), 'INT': ('h', 2), 'DINT': ('i', 4),
              'LINT': ('q', 8), 'USINT': ('B', 1), 'UINT': ('H', 2), 'UDINT': ('I', 4),
              'ULINT': ('Q', 8), 'REAL': ('f', 4), 'LREAL': ('d', 8)}
INTEGER_TYPES = set(PRIMITIVES) - {'BOOL', 'REAL', 'LREAL'}
UNSUPPORTED = {'MESSAGE', 'ALARM_DIGITAL', 'AXIS_SERVO', 'PID_ENHANCED'}


class UnresolvedValue(ValueError):
    pass


@dataclass
class LayoutMember:
    name: str
    data_type: str
    offset: int
    bit: object
    dimensions: tuple
    hidden: bool


@dataclass
class Layout:
    name: str
    size: int
    members: list
    string_capacity: object = None


def load_layouts(path, problems):
    layouts = {}
    if not Path(path).exists():
        return layouts
    try:
        elements = ET.parse(path).getroot().findall('DataTypes/DataType')
    except ET.ParseError as exc:
        problems.append(dict(code='tag-layout', owner='TagInfo', reason=str(exc)))
        return layouts
    seen = set()
    for element in elements:
        name = element.get('Name', '')
        key = name.upper()
        try:
            if not key or key in seen:
                raise ValueError('Missing or duplicate type identity')
            seen.add(key)
            members = [
                LayoutMember(member.attrib['Name'], member.attrib['DataType'], int(member.attrib['Offset']),
                             int(member.attrib['Bit']) if 'Bit' in member.attrib else None,
                             tuple(int(d.attrib['Size']) for d in member.findall('Dimensions/Dim')),
                             member.get('Hidden') == 'true')
                for member in element.findall('Members/Member')
            ]
            if len({m.name.upper() for m in members}) != len(members):
                raise ValueError('Duplicate member identity')
            string = element.find('String')
            layout = Layout(name, int(element.attrib['Size']), members,
                            int(string.attrib['Length']) if string is not None else None)
            if layout.size < 0 or any(m.offset < 0 or len(m.dimensions) > 3
                                      or any(n <= 0 for n in m.dimensions)
                                      or (m.bit is not None and (m.bit < 0 or m.dimensions
                                          or m.data_type.upper() != 'BOOL')) for m in members):
                raise ValueError('Unsupported member dimensions, bit or offset')
            if layout.string_capacity is not None and not 0 <= layout.string_capacity <= layout.size - 4:
                raise ValueError('String capacity exceeds its containing layout')
            layouts[key] = layout
        except (KeyError, ValueError) as exc:
            layouts.pop(key, None)
            problems.append(dict(code='tag-layout', owner=name or 'TagInfo', reason=str(exc)))
    return layouts


def repair_bit_targets(data_types, layouts, problems):
    """Require one integer storage field covering the bit address."""
    for dt in data_types.values():
        if dt.cls != 'User':
            continue
        layout = layouts.get(dt.name.upper())
        if not layout:
            continue
        by_name = {m.name: m for m in layout.members}
        for member in dt.members:
            evidence = by_name.get(member.name)
            if member.data_type not in ('BOOL', 'BIT') or evidence is None or evidence.bit is None:
                continue
            address = evidence.offset * 8 + evidence.bit
            candidates = [m for m in layout.members if m.bit is None
                          and m.data_type.upper() in INTEGER_TYPES and not m.dimensions
                          and m.offset * 8 <= address < m.offset * 8 + PRIMITIVES[m.data_type.upper()][1] * 8]
            if len(candidates) == 1:
                member.data_type = 'BIT'
                member.target, member.bit_number = candidates[0].name, address - candidates[0].offset * 8
                member.dimension = 0
            else:
                member.target = member.bit_number = None
                problems.append(dict(code='bit-backing', owner=dt.name + '/' + member.name,
                                     reason='No unique integer storage member covers the bit'))


def _read(data, offset, size):
    if offset < 0 or offset + size > len(data):
        raise UnresolvedValue(f'truncated value: need bytes {offset}:{offset + size}, have {len(data)}')
    return data[offset:offset + size]


def _cdata(text):
    return '<![CDATA[' + text.replace(']]>', ']]]]><![CDATA[>') + ']]>'


def _escape_byte(value):
    # Reconciled Logix string encoding from the supplied previous pipeline.
    return {36: '$$', 39: "$'", 13: '$r', 10: '$l', 9: '$t', 12: '$p'}.get(
        value, chr(value) if 32 <= value <= 126 else f'${value:02X}')


def _real(value, kind):
    if not math.isfinite(value):
        raise UnresolvedValue('non-finite REAL encoding is not yet verified')
    text = f'{value:.8e}' if kind == 'REAL' else f'{value:.17e}'
    return re.sub(r'e([+-])(\d+)$', lambda m: 'e' + m[1] + m[2].zfill(3), text)


class ValueEncoder:
    def __init__(self, layouts, data_types, aoi_types=()):
        self.layouts, self.data_types = layouts, data_types
        self.aoi_types = aoi_types

    def size(self, kind, stack=()):
        upper = kind.upper()
        if upper in UNSUPPORTED:
            raise UnresolvedValue(f'{kind} configuration format is not decoded')
        if upper in PRIMITIVES:
            return PRIMITIVES[upper][1]
        if upper not in self.layouts:
            raise UnresolvedValue(f'missing layout for {kind}')
        if upper in stack:
            raise UnresolvedValue(f'recursive layout for {kind}')
        return self.layouts[upper].size

    def decode(self, kind, data, offset=0, dimensions=(), stack=()):
        upper = kind.upper()
        if dimensions:
            count = math.prod(dimensions)
            if count < 0 or count > 1000000:
                raise UnresolvedValue('unsupported array size')
            size = self.size(kind, stack)
            if upper == 'BOOL':
                packed = _read(data, offset, ((count + 31) // 32) * 4)
                values = [('scalar', 'BOOL', (packed[i // 8] >> (i % 8)) & 1) for i in range(count)]
            else:
                _read(data, offset, count * size)
                values = [self.decode(kind, data, offset + i * size, stack=stack) for i in range(count)]
            return 'array', kind, dimensions, values
        if upper in UNSUPPORTED:
            raise UnresolvedValue(f'{kind} configuration format is not decoded')
        if upper in PRIMITIVES:
            fmt, size = PRIMITIVES[upper]
            value = struct.unpack('<' + fmt, _read(data, offset, size))[0]
            if upper == 'BOOL' and value not in (0, 1):
                raise UnresolvedValue('invalid BOOL storage value')
            if upper in ('REAL', 'LREAL') and not math.isfinite(value):
                raise UnresolvedValue('non-finite REAL encoding is not yet verified')
            return 'scalar', kind, value
        if upper in stack:
            raise UnresolvedValue(f'recursive layout for {kind}')
        layout = self.layouts.get(upper)
        if not layout:
            raise UnresolvedValue(f'missing layout for {kind}')
        _read(data, offset, layout.size)
        if layout.string_capacity is not None:
            if not 0 <= layout.string_capacity <= layout.size - 4:
                raise UnresolvedValue('String capacity exceeds its containing layout')
            length = struct.unpack('<i', _read(data, offset, 4))[0]
            if not 0 <= length <= layout.string_capacity:
                raise UnresolvedValue('STRING LEN is outside its stored capacity')
            return 'string', layout.name, length, _read(data, offset + 4, layout.string_capacity)
        members = []
        for member in layout.members:
            width = member.bit // 8 + 1 if member.bit is not None else self.size(member.data_type, stack + (upper,)) * (math.prod(member.dimensions) if member.dimensions else 1)
            if member.data_type.upper() == 'BOOL' and member.dimensions and member.bit is None:
                width = ((math.prod(member.dimensions) + 31) // 32) * 4
            if member.offset < 0 or member.offset + width > layout.size:
                raise UnresolvedValue('member exceeds containing layout')
            if member.bit is not None:
                byte = _read(data, offset + member.offset + member.bit // 8, 1)[0]
                node = 'scalar', 'BOOL', (byte >> (member.bit % 8)) & 1
            else:
                node = self.decode(member.data_type, data, offset + member.offset,
                                   member.dimensions, stack + (upper,))
            members.append((member, node))
        return 'structure', layout.name, members

    def l5k(self, node):
        category, kind = node[:2]
        if category == 'scalar':
            return _real(node[2], kind.upper()) if kind.upper() in ('REAL', 'LREAL') else str(node[2])
        if category == 'string':
            return f"[{node[2]},'" + ''.join(_escape_byte(b) for b in node[3]) + "']"
        if category == 'array':
            values = [('2#' if kind.upper() == 'BOOL' else '') + self.l5k(item) for item in node[3]]
            return '[' + ','.join(values) + ']'
        is_aoi = kind.upper() in self.aoi_types
        bits = [m.offset * 8 + m.bit for m, _ in node[2] if m.bit is not None]
        values = []
        for member, item in node[2]:
            if member.bit is not None:
                continue
            if is_aoi and member.hidden and re.fullmatch(r'__l[0-9A-Fa-f]{16}', member.name):
                start = member.offset * 8
                width = PRIMITIVES.get(member.data_type.upper(), (None, 0))[1] * 8
                if member.data_type.upper() not in INTEGER_TYPES or not any(start <= bit < start + width for bit in bits):
                    raise UnresolvedValue('Hidden AOI execution storage has no verified L5K slot')
            values.append(self.l5k(item))
        return '[' + ','.join(values) + ']'

    def _style(self, kind, value, radix=None):
        upper = kind.upper()
        if upper in ('REAL', 'LREAL'):
            if radix not in (None, 'Float'):
                raise UnresolvedValue(f'Unsupported {kind} display radix: {radix}')
            _real(value, upper)
            return 'Float', format(value, '.9g' if upper == 'REAL' else '.17g')
        if radix is None:
            radix = 'Decimal'
        if radix not in ('Decimal', 'Hex', 'Binary', 'Octal'):
            raise UnresolvedValue(f'Unsupported {kind} display radix: {radix}')
        if radix == 'Decimal':
            return radix, str(value)
        width = PRIMITIVES[upper][1] * 8
        value &= (1 << width) - 1
        prefix, text = {'Hex': ('16#', f'{value:0{width // 4}x}'),
                        'Binary': ('2#', f'{value:0{width}b}'), 'Octal': ('8#', f'{value:o}')}[radix]
        if radix in ('Hex', 'Binary'):
            text = '_'.join(text[max(0, len(text) - i - 4):len(text) - i]
                            for i in reversed(range(0, len(text), 4)))
        return radix, prefix + text

    def decorated(self, node, member_name=None, radix=None):
        category, kind = node[:2]
        name = f' Name={quoteattr(member_name)}' if member_name is not None else ''
        if category == 'scalar':
            style, value = self._style(kind, node[2], radix)
            style_attr = '' if kind.upper() == 'BOOL' and member_name is not None else f' Radix="{style}"'
            tag = 'DataValueMember' if member_name is not None else 'DataValue'
            return f'<{tag}{name} DataType={quoteattr(kind)}{style_attr} Value={quoteattr(value)}/>'
        if category == 'string':
            if member_name is None:
                return ''
            # Nested strings retain explicit LEN and byte data, not replacement text.
            active = ''.join(_escape_byte(b) for b in node[3][:node[2]])
            return (f'<StructureMember{name} DataType={quoteattr(kind)}>'
                    f'<DataValueMember Name="LEN" DataType="DINT" Radix="Decimal" Value="{node[2]}"/>'
                    f'<DataValueMember Name="DATA" DataType={quoteattr(kind)} Radix="ASCII">'
                    + _cdata("'" + active + "'" if active else '') + '</DataValueMember>'
                    '</StructureMember>')
        if category == 'array':
            tag = 'ArrayMember' if member_name is not None else 'Array'
            dimensions = ','.join(map(str, node[2]))
            primitive = kind.upper() in PRIMITIVES
            style = self._style(kind, 0, radix)[0] if primitive else None
            style_attr = f' Radix="{style}"' if style else ''
            items = []
            for index, item in zip(itertools.product(*(range(n) for n in node[2])), node[3]):
                coordinate = '[' + ','.join(map(str, index)) + ']'
                if item[0] == 'scalar':
                    value = self._style(kind, item[2], radix)[1]
                    items.append(f'<Element Index="{coordinate}" Value={quoteattr(value)}/>')
                elif item[0] == 'string':
                    content = self.decorated(item, '_string').replace('<StructureMember Name="_string"', '<Structure', 1).replace('</StructureMember>', '</Structure>')
                    items.append(f'<Element Index="{coordinate}">{content}</Element>')
                else:
                    items.append(f'<Element Index="{coordinate}">{self.decorated(item)}</Element>')
            return f'<{tag}{name} DataType={quoteattr(kind)} Dimensions="{dimensions}"{style_attr}>' + ''.join(items) + f'</{tag}>'
        tag = 'StructureMember' if member_name is not None else 'Structure'
        dt = self.data_types.get(kind.upper())
        members = {m.name: m for m in dt.members} if dt else {}
        parts = []
        for member, item in node[2]:
            if not member.hidden:
                declared = members.get(member.name)
                parts.append(self.decorated(item, member.name, getattr(declared, 'radix', None)))
        return f'<{tag}{name} DataType={quoteattr(kind)}>' + ''.join(parts) + f'</{tag}>'

    def encode(self, kind, data, dimensions=None, radix=None):
        if not kind:
            raise UnresolvedValue('Missing declared data type')
        shape = parse_dimensions(dimensions)
        if len(shape) > 3 or any(n <= 0 for n in shape):
            raise UnresolvedValue('invalid array dimensions')
        count = math.prod(shape) if shape else 1
        expected = ((count + 31) // 32) * 4 if shape and kind.upper() == 'BOOL' else count * self.size(kind)
        if len(data) != expected:
            raise UnresolvedValue('value buffer size does not match declared layout')
        node = self.decode(kind, data, dimensions=shape)
        l5k = '<Data Format="L5K">' + _cdata(self.l5k(node)) + '</Data>'
        if node[0] == 'string':
            active = ''.join(_escape_byte(b) for b in node[3][:node[2]])
            return l5k + f'<Data Format="String" Length="{node[2]}">' + _cdata("'" + active + "'") + '</Data>'
        return l5k + '<Data Format="Decorated">' + self.decorated(node, radix=radix) + '</Data>'


def attach_values(controller, cursor, layouts, problems):
    encoder = ValueEncoder(layouts, {}, {aoi.name.upper() for aoi in controller.aois})
    unresolved = []
    owners = [('Controller', controller.tags)] + [('Program:' + p.name, p.tags) for p in controller.programs]
    cache = {}
    all_types = {dt.name.upper(): dt for dt in controller.data_types}
    for _, tags in owners:
        for tag in tags:
            all_types.update(tag._data_types_map)
    repair_bit_targets(all_types, layouts, problems)
    encoder.data_types = all_types
    for owner, tags in owners:
        for tag in tags:
            tag._value_xml = ''
            if owner != 'Controller':
                tag.cls = None
            if tag.tag_type == 'Alias' or getattr(tag, '_l5x_exclude', False) or ((tag.data_type or '').upper() in PRIMITIVES and not ((tag.data_type or '').upper() == 'BOOL' and tag.dimensions)):
                continue
            try:
                tag._value_problem = None
                dti = tag._data_table_instance
                if dti not in cache:
                    cache[dti] = source_value(cursor, dti)
                data = cache[dti]
                tag._value_xml = encoder.encode(tag.data_type, data, tag.dimensions, tag.radix)
            except (ValueError, KeyError, EOFError, struct.error) as exc:
                tag._value_problem = str(exc)
                unresolved.append({'owner': owner, 'tag': tag.name, 'data_type': tag.data_type, 'reason': str(exc)})
    controller._unresolved_values = unresolved
    if unresolved:
        log.warning('{} tag values remain unresolved; no values were invented', len(unresolved))
