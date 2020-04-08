import re
import sys

from webencodings import ascii_lower

from .ast import (
    AtKeywordToken, Comment, CurlyBracketsBlock, DimensionToken, FunctionBlock,
    HashToken, IdentToken, LiteralToken, NumberToken, ParenthesesBlock,
    ParseError, PercentageToken, SquareBracketsBlock, StringToken,
    UnicodeRangeToken, URLToken, WhitespaceToken)
from .serializer import serialize_string_value, serialize_url

_NUMBER_RE = re.compile(r'[-+]?([0-9]*\.)?[0-9]+([eE][+-]?[0-9]+)?')
_HEX_ESCAPE_RE = re.compile(r'([0-9A-Fa-f]{1,6})[ \n\t]?')


def parse_component_value_list(css, skip_comments=False):
    """Parse a list of component values.

    :type css: :obj:`str`
    :param css: A CSS string.
    :type skip_comments: :obj:`bool`
    :param skip_comments:
        Ignore CSS comments.
        The return values (and recursively its blocks and functions)
        will not contain any :class:`~tinycss2.ast.Comment` object.
    :returns: A list of :term:`component values`.

    """
    css = (css.replace('\0', '\uFFFD')
           # This turns out to be faster than a regexp:
           .replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n'))
    length = len(css)
    token_start_pos = pos = 0  # Character index in the css source.
    line = 1  # First line is line 1.
    last_newline = -1
    root = tokens = []
    end_char = None  # Pop the stack when encountering this character.
    stack = []  # Stack of nested blocks: (tokens, end_char) tuples.

    while pos < length:
        newline = css.rfind('\n', token_start_pos, pos)
        if newline != -1:
            line += 1 + css.count('\n', token_start_pos, newline)
            last_newline = newline
        # First character in a line is in column 1.
        column = pos - last_newline
        token_start_pos = pos
        c = css[pos]

        if c in ' \n\t':
            pos += 1
            while css.startswith((' ', '\n', '\t'), pos):
                pos += 1
            value = css[token_start_pos:pos]
            tokens.append(WhitespaceToken(line, column, value))
            continue
        elif (c in 'Uu' and pos + 2 < length and css[pos + 1] == '+' and
              css[pos + 2] in '0123456789abcdefABCDEF?'):
            start, end, pos = _consume_unicode_range(css, pos + 2)
            tokens.append(UnicodeRangeToken(line, column, start, end))
            continue
        elif css.startswith('-->', pos):  # Check before identifiers
            tokens.append(LiteralToken(line, column, '-->'))
            pos += 3
            continue
        elif _is_ident_start(css, pos):
            value, pos = _consume_ident(css, pos)
            if not css.startswith('(', pos):  # Not a function
                tokens.append(IdentToken(line, column, value))
                continue
            pos += 1  # Skip the '('
            if ascii_lower(value) == 'url':
                url_pos = pos
                while css.startswith((' ', '\n', '\t'), url_pos):
                    url_pos += 1
                if url_pos >= length or css[url_pos] not in ('"', "'"):
                    value, pos, error = _consume_url(css, pos)
                    if value is not None:
                        repr = 'url({})'.format(serialize_url(value))
                        if error is not None:
                            error_key = error[0]
                            if error_key == 'eof-in-string':
                                repr = repr[:-2]
                            else:
                                assert error_key == 'eof-in-url'
                                repr = repr[:-1]
                        tokens.append(URLToken(line, column, value, repr))
                    if error is not None:
                        tokens.append(ParseError(line, column, *error))
                    continue
            arguments = []
            tokens.append(FunctionBlock(line, column, value, arguments))
            stack.append((tokens, end_char))
            end_char = ')'
            tokens = arguments
            continue

        match = _NUMBER_RE.match(css, pos)
        if match:
            pos = match.end()
            repr_ = css[token_start_pos:pos]
            value = float(repr_)
            int_value = int(repr_) if not any(match.groups()) else None
            if pos < length and _is_ident_start(css, pos):
                unit, pos = _consume_ident(css, pos)
                tokens.append(DimensionToken(
                    line, column, value, int_value, repr_, unit))
            elif css.startswith('%', pos):
                pos += 1
                tokens.append(PercentageToken(
                    line, column, value, int_value, repr_))
            else:
                tokens.append(NumberToken(
                    line, column, value, int_value, repr_))
        elif c == '@':
            pos += 1
            if pos < length and _is_ident_start(css, pos):
                value, pos = _consume_ident(css, pos)
                tokens.append(AtKeywordToken(line, column, value))
            else:
                tokens.append(LiteralToken(line, column, '@'))
        elif c == '#':
            pos += 1
            if pos < length and (
                    css[pos] in '0123456789abcdefghijklmnopqrstuvwxyz'
                                '-_ABCDEFGHIJKLMNOPQRSTUVWXYZ' or
                    ord(css[pos]) > 0x7F or  # Non-ASCII
                    # Valid escape:
                    (css[pos] == '\\' and not css.startswith('\\\n', pos))):
                is_identifier = _is_ident_start(css, pos)
                value, pos = _consume_ident(css, pos)
                tokens.append(HashToken(line, column, value, is_identifier))
            else:
                tokens.append(LiteralToken(line, column, '#'))
        elif c == '{':
            content = []
            tokens.append(CurlyBracketsBlock(line, column, content))
            stack.append((tokens, end_char))
            end_char = '}'
            tokens = content
            pos += 1
        elif c == '[':
            content = []
            tokens.append(SquareBracketsBlock(line, column, content))
            stack.append((tokens, end_char))
            end_char = ']'
            tokens = content
            pos += 1
        elif c == '(':
            content = []
            tokens.append(ParenthesesBlock(line, column, content))
            stack.append((tokens, end_char))
            end_char = ')'
            tokens = content
            pos += 1
        elif c == end_char:  # Matching }, ] or )
            # The top-level end_char is None (never equal to a character),
            # so we never get here if the stack is empty.
            tokens, end_char = stack.pop()
            pos += 1
        elif c in '}])':
            tokens.append(ParseError(line, column, c, 'Unmatched ' + c))
            pos += 1
        elif c in ('"', "'"):
            value, pos, error = _consume_quoted_string(css, pos)
            if value is not None:
                repr = '"{}"'.format(serialize_string_value(value))
                if error is not None:
                    repr = repr[:-1]
                tokens.append(StringToken(line, column, value, repr))
            if error is not None:
                tokens.append(ParseError(line, column, *error))
        elif css.startswith('/*', pos):  # Comment
            pos = css.find('*/', pos + 2)
            if pos == -1:
                if not skip_comments:
                    tokens.append(
                        Comment(line, column, css[token_start_pos + 2:]))
                break
            if not skip_comments:
                tokens.append(
                    Comment(line, column, css[token_start_pos + 2:pos]))
            pos += 2
        elif css.startswith('<!--', pos):
            tokens.append(LiteralToken(line, column, '<!--'))
            pos += 4
        elif css.startswith('||', pos):
            tokens.append(LiteralToken(line, column, '||'))
            pos += 2
        elif c in '~|^$*':
            pos += 1
            if css.startswith('=', pos):
                pos += 1
                tokens.append(LiteralToken(line, column, c + '='))
            else:
                tokens.append(LiteralToken(line, column, c))
        else:
            tokens.append(LiteralToken(line, column, c))
            pos += 1
    return root


def _is_name_start(css, pos):
    """Return true if the given character is a name-start code point."""
    # https://www.w3.org/TR/css-syntax-3/#name-start-code-point
    c = css[pos]
    return (
        c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_' or
        ord(c) > 0x7F)


def _is_ident_start(css, pos):
    """Return True if the given position is the start of a CSS identifier."""
    # https://drafts.csswg.org/css-syntax/#would-start-an-identifier
    if _is_name_start(css, pos):
        return True
    elif css[pos] == '-':
        pos += 1
        return (
            # Name-start code point or hyphen:
            (pos < len(css) and (
                _is_name_start(css, pos) or css[pos] == '-')) or
            # Valid escape:
            (css.startswith('\\', pos) and not css.startswith('\\\n', pos)))
    elif css[pos] == '\\':
        return not css.startswith('\\\n', pos)
    return False


def _consume_ident(css, pos):
    """Return (unescaped_value, new_pos).

    Assumes pos starts at a valid identifier. See :func:`_is_ident_start`.

    """
    # http://dev.w3.org/csswg/css-syntax/#consume-a-name
    chunks = []
    length = len(css)
    start_pos = pos
    while pos < length:
        c = css[pos]
        if c in ('abcdefghijklmnopqrstuvwxyz-_0123456789'
                 'ABCDEFGHIJKLMNOPQRSTUVWXYZ') or ord(c) > 0x7F:
            pos += 1
        elif c == '\\' and not css.startswith('\\\n', pos):
            # Valid escape
            chunks.append(css[start_pos:pos])
            c, pos = _consume_escape(css, pos + 1)
            chunks.append(c)
            start_pos = pos
        else:
            break
    chunks.append(css[start_pos:pos])
    return ''.join(chunks), pos


def _consume_quoted_string(css, pos):
    """Return (unescaped_value, new_pos)."""
    # http://dev.w3.org/csswg/css-syntax/#consume-a-string-token
    error = None
    quote = css[pos]
    assert quote in ('"', "'")
    pos += 1
    chunks = []
    length = len(css)
    start_pos = pos
    while pos < length:
        c = css[pos]
        if c == quote:
            chunks.append(css[start_pos:pos])
            pos += 1
            break
        elif c == '\\':
            chunks.append(css[start_pos:pos])
            pos += 1
            if pos < length:
                if css[pos] == '\n':  # Ignore escaped newlines
                    pos += 1
                else:
                    c, pos = _consume_escape(css, pos)
                    chunks.append(c)
            # else: Escaped EOF, do nothing
            start_pos = pos
        elif c == '\n':  # Unescaped newline
            return None, pos, ('bad-string', 'Bad string token')
        else:
            pos += 1
    else:
        error = ('eof-in-string', 'EOF in string')
        chunks.append(css[start_pos:pos])
    return ''.join(chunks), pos, error


def _consume_escape(css, pos):
    r"""Return (unescaped_char, new_pos).

    Assumes a valid escape: pos is just after '\' and not followed by '\n'.

    """
    # http://dev.w3.org/csswg/css-syntax/#consume-an-escaped-character
    hex_match = _HEX_ESCAPE_RE.match(css, pos)
    if hex_match:
        codepoint = int(hex_match.group(1), 16)
        return (
            chr(codepoint) if 0 < codepoint <= sys.maxunicode else '\uFFFD',
            hex_match.end())
    elif pos < len(css):
        return css[pos], pos + 1
    else:
        return '\uFFFD', pos


def _consume_url(css, pos):
    """Return (unescaped_url, new_pos)

    The given pos is assumed to be just after the '(' of 'url('.

    """
    error = None
    length = len(css)
    # http://dev.w3.org/csswg/css-syntax/#consume-a-url-token
    # Skip whitespace
    while css.startswith((' ', '\n', '\t'), pos):
        pos += 1
    if pos >= length:  # EOF
        return '', pos, ('eof-in-url', 'EOF in URL')
    c = css[pos]
    if c in ('"', "'"):
        value, pos, error = _consume_quoted_string(css, pos)
    elif c == ')':
        return '', pos + 1, error
    else:
        chunks = []
        start_pos = pos
        while 1:
            if pos >= length:  # EOF
                chunks.append(css[start_pos:pos])
                return ''.join(chunks), pos, ('eof-in-url', 'EOF in URL')
            c = css[pos]
            if c == ')':
                chunks.append(css[start_pos:pos])
                pos += 1
                return ''.join(chunks), pos, error
            elif c in ' \n\t':
                chunks.append(css[start_pos:pos])
                value = ''.join(chunks)
                pos += 1
                break
            elif c == '\\' and not css.startswith('\\\n', pos):
                # Valid escape
                chunks.append(css[start_pos:pos])
                c, pos = _consume_escape(css, pos + 1)
                chunks.append(c)
                start_pos = pos
            elif (c in
                  '"\'('
                  # http://dev.w3.org/csswg/css-syntax/#non-printable-character
                  '\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0e'
                  '\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19'
                  '\x1a\x1b\x1c\x1d\x1e\x1f\x7f'):
                value = None  # Parse error
                pos += 1
                break
            else:
                pos += 1

    if value is not None:
        while css.startswith((' ', '\n', '\t'), pos):
            pos += 1
        if pos < length:
            if css[pos] == ')':
                return value, pos + 1, error
        else:
            if error is None:
                error = ('eof-in-url', 'EOF in URL')
            return value, pos, error

    # http://dev.w3.org/csswg/css-syntax/#consume-the-remnants-of-a-bad-url0
    while pos < length:
        if css.startswith('\\)', pos):
            pos += 2
        elif css[pos] == ')':
            pos += 1
            break
        else:
            pos += 1
    return None, pos, ('bad-url', 'bad URL token')


def _consume_unicode_range(css, pos):
    """Return (range, new_pos)

    The given pos is assume to be just after the '+' of 'U+' or 'u+'.

    """
    # http://dev.w3.org/csswg/css-syntax/#consume-a-unicode-range-token
    length = len(css)
    start_pos = pos
    max_pos = min(pos + 6, length)
    while pos < max_pos and css[pos] in '0123456789abcdefABCDEF':
        pos += 1
    start = css[start_pos:pos]

    start_pos = pos
    # Same max_pos as before: total of hex digits and question marks <= 6
    while pos < max_pos and css[pos] == '?':
        pos += 1
    question_marks = pos - start_pos

    if question_marks:
        end = start + 'F' * question_marks
        start = start + '0' * question_marks
    elif (pos + 1 < length and css[pos] == '-' and
          css[pos + 1] in '0123456789abcdefABCDEF'):
        pos += 1
        start_pos = pos
        max_pos = min(pos + 6, length)
        while pos < max_pos and css[pos] in '0123456789abcdefABCDEF':
            pos += 1
        end = css[start_pos:pos]
    else:
        end = start
    return int(start, 16), int(end, 16), pos
