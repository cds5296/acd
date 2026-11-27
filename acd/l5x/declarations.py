"""Retain complete declarations when an editor stub repeats the same name."""


def live_tag_records(cur, collection_id):
    return cur.execute(
        "SELECT comp_name, object_id, parent_id, record_type FROM comps "
        "WHERE parent_id=? AND record_type!=512 ORDER BY seq_number", (collection_id,)
    ).fetchall()


def unique_tags(tags):
    result = {}
    for tag in tags:
        key = tag.name.casefold()
        previous = result.get(key)
        complete = bool(tag.data_type or tag.alias_for)
        if previous is not None:
            previous_complete = bool(previous.data_type or previous.alias_for)
            if complete and previous_complete:
                fields = ('name', 'data_type', 'tag_type', 'alias_for', 'dimensions', '_data_table_instance',
                          'constant', 'external_access', 'radix', 'produce_info', 'consume_info', '_comments')
                if any(getattr(tag, f, None) != getattr(previous, f, None) for f in fields):
                    raise ValueError(f'Conflicting complete tag declarations for {tag.name}')
            if previous_complete or not complete:
                continue
        result[key] = tag
    return list(result.values())
