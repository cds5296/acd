"""Source-backed primitive values, detached from the database before export."""
import itertools
import math
import re
import struct

from loguru import logger

from acd.record.rx import RxGeneric


FORMATS = dict(BOOL='B', SINT='b', INT='h', DINT='i', LINT='q',
               USINT='B', UINT='H', UDINT='I', ULINT='Q', REAL='f', LREAL='d')


def dimensions(text):
    if text is None or text == '':
        return ()
    parts = re.split(r' *, *| +', str(text).strip())
    if len(parts) > 3 or any(not re.fullmatch(r'[0-9]+', p) or int(p) <= 0 for p in parts):
        raise ValueError('Invalid array dimensions')
    return tuple(map(int, parts))


def number_text(value, kind):
    if kind in ('REAL', 'LREAL'):
        if not math.isfinite(value):
            raise ValueError('Nonfinite floating-point value is unsupported')
        return format(value, '.9g' if kind == 'REAL' else '.17g')
    return str(value)


def numeric_xml(kind, dimension_text, raw):
    shape = dimensions(dimension_text)
    if kind == 'BOOL' and shape:
        raise ValueError('Packed BOOL arrays require a separate storage decoder')
    count = math.prod(shape) if shape else 1
    width = struct.calcsize('<' + FORMATS[kind])
    if raw is None or len(raw) != count * width:
        raise ValueError('Missing or incorrectly sized primitive value buffer')
    values = [number_text(item[0], kind) for item in
              struct.iter_unpack('<' + FORMATS[kind], raw)]
    if kind == 'BOOL' and any(v not in ('0', '1') for v in values):
        raise ValueError('Invalid BOOL value')
    # L5K stores array elements in one flat sequence; Dimensions carries shape.
    l5k = '[' + ','.join(values) + ']' if shape else values[0]
    radix = 'Float' if kind in ('REAL', 'LREAL') else 'Decimal'
    if not shape:
        decorated = f'<DataValue DataType="{kind}" Radix="{radix}" Value="{values[0]}"/>'
    else:
        indices = itertools.product(*(range(n) for n in shape))
        elements = ''.join(f'<Element Index="[{",".join(map(str, index))}]" Value="{value}"/>'
                           for index, value in zip(indices, values))
        decorated = (f'<Array DataType="{kind}" Dimensions="{",".join(map(str, shape))}" '
                     f'Radix="{radix}">{elements}</Array>')
    return f'<Data Format="L5K"><![CDATA[{l5k}]]></Data><Data Format="Decorated">{decorated}</Data>'


def source_value(cur, oid):
    if not oid:
        raise ValueError('Missing data-table instance')
    rows = cur.execute('SELECT record FROM comps WHERE object_id=?', (oid,)).fetchall()
    if len(rows) != 1:
        raise ValueError('Missing or ambiguous data-table instance')
    parsed = RxGeneric.from_bytes(bytes(rows[0][0]))
    values = [bytes(a.value) for a in parsed.extended_records if a.attribute_id == 0x66]
    if len(values) != 1:
        raise ValueError('Missing or ambiguous value attribute')
    return values[0]


def materialize(controller, cur):
    owners = [controller] + list(controller.programs)
    for owner in owners:
        for tag in owner.tags:
            kind = (tag.data_type or '').upper()
            if tag.tag_type == 'Alias' or kind not in FORMATS:
                continue
            try:
                tag._source_value_xml = numeric_xml(kind, tag.dimensions,
                    source_value(cur, tag._data_table_instance))
                tag._value_problem = None
            except (ValueError, EOFError, struct.error) as exc:
                tag._source_value_xml = ''
                tag._value_problem = str(exc)
                if not tag._l5x_exclude:
                    logger.warning("Omitting numeric data for {} / {} (instance {}): {}",
                                   owner.name, tag.name, tag._data_table_instance, exc)
