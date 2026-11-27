"""Resolve module parent keys against module identities, not profile payloads."""


def parent_names(modules):
    result = {}
    for name, parsed in modules:
        if parsed.cip_type != 0x69:
            raise ValueError('Unexpected object in module collection')
        key = parsed.comment_id
        if key in result:
            raise ValueError('Ambiguous module parent identity')
        result[key] = name
    return result
