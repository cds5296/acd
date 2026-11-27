"""Independently verified controller text and topology fields."""


def description_xml(comments):
    texts = {text.replace('\r\n', '\n') for reference, text in comments
             if reference in ('', '.') and text}
    if len(texts) > 1:
        raise ValueError('Ambiguous controller description')
    if not texts:
        return '<Description></Description>'
    text = texts.pop().replace(']]>', ']]]]><![CDATA[>')
    return '<Description><![CDATA[' + text + ']]></Description>'


def topology(raw):
    if raw is None or raw == b'\x00\x00':
        return None
    if raw != b'\x02\x00':
        raise ValueError('Unverified controller Ethernet topology')
    return 'A1/A2: Linear/DLR'
