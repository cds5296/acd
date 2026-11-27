"""Module inhibition is a configuration field, scoped by descriptor layout."""


def inhibition(record, metadata):
    fields = [a for a in record.extended_records if a.attribute_id == 1]
    if record.cip_type != 0x69 or len(fields) != 1:
        return None
    layout = getattr(record, 'record_format_version', None), len(metadata)
    if layout == (166, 946):
        return 'true' if metadata[20] & 4 else 'false'
    if layout == (173, 1504) and metadata[20] & 4 == 0:
        return 'false'
    return None
