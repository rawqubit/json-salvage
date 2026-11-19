"""Recover usable data from the JSON that language models actually emit.

Everything here is standard library.  The scanner walks the input one
character at a time and keeps an explicit stack of the containers it has
opened, so a value that was cut off mid-stream can always be closed and
returned: the repair is the same code path as the happy path.
"""

import argparse
import codecs
import json
import sys

__all__ = ["salvage", "salvage_all", "repair", "iter_salvage"]
__version__ = "1.0.0"

WHITESPACE = " \t\r\n\f\v\ufeff"
DIGITS = "0123456789"
HEXDIGITS = "0123456789abcdefABCDEF"

# opener -> the characters that close it.  Smart quotes are accepted as
# delimiters and normalised to plain double quotes on output.
QUOTE_CLOSERS = {
    '"': ('"',),
    "'": ("'",),
    "\u201c": ("\u201d", '"'),
    "\u201d": ("\u201d", '"'),
    "\u2018": ("\u2019", "'"),
    "\u2019": ("\u2019", "'"),
}

# Recognised bare words and the JSON token each one becomes.
LITERALS = {
    "true": "true",
    "false": "false",
    "null": "null",
    "none": "null",
    "nil": "null",
    "undefined": "null",
    "nan": "NaN",
    "infinity": "Infinity",
    "inf": "Infinity",
}

NO_VALUE = "no JSON value found"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _as_text(data):
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data).decode("utf-8", "replace")
    raise TypeError("expected str or bytes, got %s" % type(data).__name__)


def _dump_str(value):
    """JSON-encode a string, keeping non-ASCII readable where it is safe."""
    for ch in value:
        if "\ud800" <= ch <= "\udfff":
            return json.dumps(value)
    return json.dumps(value, ensure_ascii=False)


def _word_at(text, i, n):
    j = i
    while j < n and (text[j].isalnum() or text[j] == "_"):
        j += 1
    return text[i:j]


def _skip(text, i, n):
    """Skip whitespace, ``//`` comments and ``/* */`` comments."""
    while i < n:
        ch = text[i]
        if ch in WHITESPACE:
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i + 2)
            i = n if nl < 0 else nl + 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            break
    return i


def _read_escape(text, j, n):
    """Decode the escape starting at ``text[j] == '\\\\'``."""
    if j + 1 >= n:
        return j + 1, "\\"
    esc = text[j + 1]
    if esc in '"\'\\/':
        return j + 2, esc
    simple = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    if esc in simple:
        return j + 2, simple[esc]
    if esc == "u":
        hexs = text[j + 2:j + 6]
        if len(hexs) == 4 and all(c in HEXDIGITS for c in hexs):
            code = int(hexs, 16)
            end = j + 6
            if 0xD800 <= code <= 0xDBFF and text[end:end + 2] == "\\u":
                low_hex = text[end + 2:end + 6]
                if len(low_hex) == 4 and all(c in HEXDIGITS for c in low_hex):
                    low = int(low_hex, 16)
                    if 0xDC00 <= low <= 0xDFFF:
                        combined = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                        return end + 6, chr(combined)
            return end, chr(code)
        return j + 2, "\\u"
    return j + 2, "\\" + esc


def _read_string(text, i, n):
    """Read a quoted string starting at ``i``; return (value, next index).

    A string that runs out of input, or that runs into a blank line, is
    treated as truncated and closed where it stops.
    """
    closers = QUOTE_CLOSERS[text[i]]
    buf = []
    j = i + 1
    while j < n:
        ch = text[j]
        if ch in closers:
            return "".join(buf), j + 1
        if ch == "\\":
            j, decoded = _read_escape(text, j, n)
            buf.append(decoded)
            continue
        if ch == "\n":
            k = j + 1
            while k < n and text[k] in " \t\r":
                k += 1
            if k >= n or text[k] == "\n":
                return "".join(buf), (n if k >= n else j)
        buf.append(ch)
        j += 1
    return "".join(buf), n


def _read_number(text, i, n):
    """Read a JSON-ish number; return (json token, next index) or None."""
    j = i
    sign = ""
    if text[j] in "+-":
        if text[j] == "-":
            sign = "-"
        j += 1
    start = j
    while j < n and text[j] in DIGITS:
        j += 1
    int_part = text[start:j]
    frac = None
    if j < n and text[j] == ".":
        k = j + 1
        while k < n and text[k] in DIGITS:
            k += 1
        if k > j + 1:
            frac = text[j + 1:k]
            j = k
        else:
            frac = ""
            j += 1
    exp = None
    if j < n and text[j] in "eE":
        k = j + 1
        if k < n and text[k] in "+-":
            k += 1
        digits_at = k
        while k < n and text[k] in DIGITS:
            k += 1
        if k > digits_at:
            exp = text[j:k]
            j = k
    if not int_part and not frac:
        return None
    if int_part:
        int_part = int_part.lstrip("0") or "0"
    else:
        int_part = "0"
    token = sign + int_part
    if frac is not None:
        token += "." + (frac or "0")
    if exp is not None:
        token += exp
    return token, j


def _read_key(text, i, n):
    """Read an object key; return (key or None, next index)."""
    if text[i] in QUOTE_CLOSERS:
        return _read_string(text, i, n)
    j = i
    while j < n:
        ch = text[j]
        if ch == ":":
            key = text[i:j].strip()
            return (key or None), j
        if ch in ',{}[]"\'\r\n':
            break
        j += 1
    j = i
    while j < n and text[j] not in " \t\r\n,{}[]:":
        j += 1
    key = text[i:j].strip()
    return (key or None), j


# --------------------------------------------------------------------------
# the scanner
# --------------------------------------------------------------------------

def _drop_member(out, frame):
    """Throw away the half-written member an object or array has started."""
    if frame[1] is not None:
        del out[frame[1]:]
        frame[1] = None


def _finish(out, stack, i):
    """Close every open container, dropping any member that never got a value."""
    while stack:
        kind, member, _ = stack.pop()
        if member is not None:
            del out[member:]
        out.append("}" if kind == "obj" else "]")
        if stack:
            stack[-1][1] = None
    if not out:
        return None
    return "".join(out), i


def _parse_value(text, start):
    """Scan one value at ``start``; return (json text, end index) or None."""
    n = len(text)
    out = []
    # each frame is [kind, out-index where the pending member began, has members]
    stack = []
    i = start
    state = "value"

    while True:
        if state == "value":
            i = _skip(text, i, n)
            if i >= n:
                return _finish(out, stack, n)
            frame = stack[-1] if stack else None
            ch = text[i]
            if frame is not None and ch in "}]":
                if ch == ("}" if frame[0] == "obj" else "]"):
                    _drop_member(out, frame)
                    out.append(ch)
                    stack.pop()
                    i += 1
                    if stack:
                        stack[-1][1] = None
                    state = "after" if stack else "done"
                    continue
                return _finish(out, stack, i)
            if ch == ",":
                i += 1
                continue
            if frame is not None and frame[0] == "arr" and frame[1] is None:
                frame[1] = len(out)
                if frame[2]:
                    out.append(", ")
                frame[2] = True
            if ch in QUOTE_CLOSERS:
                value, i = _read_string(text, i, n)
                out.append(_dump_str(value))
            elif ch == "{" or ch == "[":
                out.append(ch)
                stack.append(["obj" if ch == "{" else "arr", None, False])
                i += 1
                state = "key" if ch == "{" else "value"
                continue
            elif ch in "-+" or ch in DIGITS or ch == ".":
                nxt = text[i + 1] if i + 1 < n else ""
                if ch in "-+" and nxt.isalpha():
                    word = _word_at(text, i + 1, n).lower()
                    if word not in ("infinity", "inf"):
                        return _finish(out, stack, i)
                    out.append(("" if ch == "+" else "-") + "Infinity")
                    i += 1 + len(word)
                else:
                    parsed = _read_number(text, i, n)
                    if parsed is None:
                        return _finish(out, stack, i)
                    token, i = parsed
                    out.append(token)
            elif ch.isalpha() or ch in "_":
                word = _word_at(text, i, n)
                mapped = LITERALS.get(word.lower())
                if mapped is None:
                    return _finish(out, stack, i)
                out.append(mapped)
                i += len(word)
            else:
                return _finish(out, stack, i)
            frame = stack[-1] if stack else None
            if frame is not None:
                frame[1] = None
            state = "after" if stack else "done"
            continue

        if state == "key":
            i = _skip(text, i, n)
            if i >= n:
                return _finish(out, stack, n)
            frame = stack[-1]
            ch = text[i]
            if ch == "}":
                out.append("}")
                stack.pop()
                i += 1
                if stack:
                    stack[-1][1] = None
                state = "after" if stack else "done"
                continue
            if ch == ",":
                i += 1
                continue
            key, j = _read_key(text, i, n)
            if key is None:
                return _finish(out, stack, i)
            frame[1] = len(out)
            if frame[2]:
                out.append(", ")
            frame[2] = True
            out.append(_dump_str(key))
            i = _skip(text, j, n)
            if i < n and text[i] == ":":
                i += 1
            out.append(": ")
            state = "value"
            continue

        if state == "after":
            i = _skip(text, i, n)
            if i >= n:
                return _finish(out, stack, n)
            frame = stack[-1]
            ch = text[i]
            closer = "}" if frame[0] == "obj" else "]"
            if ch == closer:
                out.append(ch)
                stack.pop()
                i += 1
                if stack:
                    stack[-1][1] = None
                state = "after" if stack else "done"
                continue
            if ch == ",":
                i += 1
                frame[1] = None
                state = "key" if frame[0] == "obj" else "value"
                continue
            state = "key" if frame[0] == "obj" else "value"
            continue

        # state == "done"
        return "".join(out), i


def _find_container(text, i, n):
    """Find the next object or array opening that is not part of a word."""
    while i < n:
        ch = text[i]
        if ch == "{" or ch == "[":
            if not (i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")):
                return i
        i += 1
    return -1


def _find_start(text, i, n):
    """Find the next position where a value plausibly begins, or -1."""
    while i < n:
        ch = text[i]
        if ch in WHITESPACE:
            i += 1
            continue
        if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
            i += 1
            continue
        if ch == "{" or ch == "[":
            return i
        if ch in QUOTE_CLOSERS:
            if ch in ("'", "\u2018", "\u2019"):
                j = i + 1
                closed = False
                while j < n and text[j] not in "\r\n":
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] in ("'", "\u2019"):
                        closed = True
                        break
                    j += 1
                if not closed:
                    i += 1
                    continue
            return i
        if ch in DIGITS or ch in "+-.":
            nxt = text[i + 1] if i + 1 < n else ""
            if ch in "-+":
                word = _word_at(text, i + 1, n).lower()
                if word in ("infinity", "inf"):
                    return i
                if not (nxt.isdigit() or nxt == "."):
                    i += 1
                    continue
            elif not (ch.isdigit() or nxt.isdigit()):
                i += 1
                continue
            return i
        if ch.isalpha() or ch == "_":
            word = _word_at(text, i, n).lower()
            if word in LITERALS:
                return i
            i += 1
            continue
        i += 1
    return -1


def _first_value(text):
    """Return (json text, python value, end index) for the first value, or None.

    A scalar that opens the document is taken at face value, but a scalar that
    only appears after prose loses to an object or array further along, so
    "here are 3 fields: {...}" does not recover 3.
    """
    n = len(text)
    pos = _find_start(text, 0, n)
    if pos < 0:
        return None
    starts = [pos]
    if text[pos] not in "{[" and text[:pos].strip():
        later = _find_container(text, pos + 1, n)
        if later > pos:
            starts = [later, pos]
    for first in starts:
        cur = first
        while cur >= 0:
            result = _parse_value(text, cur)
            if result is not None:
                try:
                    value = json.loads(result[0])
                except Exception:
                    pass
                else:
                    return result[0], value, result[1]
            cur = _find_start(text, cur + 1, n)
    return None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def salvage(text):
    """Return the first recoverable JSON value in *text*.

    Raises ValueError('no JSON value found') when there is nothing to recover.
    """
    found = _first_value(_as_text(text))
    if found is None:
        raise ValueError(NO_VALUE)
    return found[1]


def salvage_all(text):
    """Return every recoverable JSON value in *text*, in document order.

    Never raises: input with nothing recoverable returns an empty list.
    """
    try:
        text = _as_text(text)
    except TypeError:
        return []
    out = []
    n = len(text)
    start = 0
    while start < n:
        pos = _find_start(text, start, n)
        if pos < 0:
            break
        end = pos + 1
        try:
            result = _parse_value(text, pos)
        except Exception:
            result = None
        if result is not None:
            try:
                out.append(json.loads(result[0]))
            except Exception:
                pass
            else:
                end = max(result[1], pos + 1)
        start = end
    return out


def repair(text):
    """Return canonical JSON text for the first recoverable value in *text*.

    Raises ValueError('no JSON value found') when there is nothing to recover.
    """
    found = _first_value(_as_text(text))
    if found is None:
        raise ValueError(NO_VALUE)
    return found[0]


def iter_salvage(chunks):
    """Yield the newest repaired value each time it grows.

    Consumes an iterable of string chunks and repairs the accumulated buffer
    after every chunk.  A value is yielded only when it differs from the last
    one yielded and serialises longer than it, so a partially built object is
    rendered once per step and never falls back to a smaller snapshot while a
    member is half-written.  The value for the complete buffer is always
    yielded when the chunks run out.
    """
    parts = []
    last = None
    pending = None
    for chunk in chunks:
        if not isinstance(chunk, str):
            chunk = _as_text(chunk)
        if not chunk:
            continue
        parts.append(chunk)
        try:
            value = salvage("".join(parts))
        except ValueError:
            continue
        serialized = json.dumps(value, sort_keys=False)
        if serialized == last:
            pending = None
            continue
        if last is not None and len(serialized) <= len(last):
            # A half-written member was dropped and the value fell back to its
            # last complete state.  Hold it back rather than rendering it.
            pending = (value, serialized)
            continue
        last = serialized
        pending = None
        yield value
    if pending is not None:
        yield pending[0]


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _stdin_chunks(size=64):
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:  # stdin replaced by a text stream (tests, pipes)
        while True:
            data = sys.stdin.read(size)
            if not data:
                return
            yield data
    while True:
        data = stream.read(size)
        if not data:
            tail = decoder.decode(b"", True)
            if tail:
                yield tail
            return
        chunk = decoder.decode(data)
        if chunk:
            yield chunk


def _render(value, compact):
    if compact:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(value, indent=2, ensure_ascii=False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="json-salvage",
        description="Recover usable data from broken JSON.",
    )
    parser.add_argument("file", help="file to read, or - for stdin")
    parser.add_argument("--all", action="store_true", help="print every value, one per line")
    parser.add_argument("--compact", action="store_true", help="compact instead of pretty")
    parser.add_argument("--stream", action="store_true", help="print each repaired snapshot as stdin arrives")
    parser.add_argument("--quiet", action="store_true", help="print nothing, only set the exit code")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    try:
        if args.stream:
            seen = 0
            for value in iter_salvage(_stdin_chunks()):
                seen += 1
                if not args.quiet:
                    sys.stdout.write(_render(value, True) + "\n")
            if seen:
                return 0
            if not args.quiet:
                sys.stderr.write("json-salvage: " + NO_VALUE + "\n")
            return 1

        text = sys.stdin.read() if args.file == "-" else _read_text(args.file)
    except OSError as exc:
        sys.stderr.write("json-salvage: cannot read %s: %s\n" % (args.file, exc.strerror or exc))
        return 1

    if args.all:
        values = salvage_all(text)
        if not args.quiet:
            for value in values:
                sys.stdout.write(_render(value, True) + "\n")
        if values:
            return 0
        if not args.quiet:
            sys.stderr.write("json-salvage: " + NO_VALUE + "\n")
        return 1

    try:
        value = salvage(text)
    except ValueError:
        if not args.quiet:
            sys.stderr.write("json-salvage: " + NO_VALUE + "\n")
        return 1
    if not args.quiet:
        sys.stdout.write(_render(value, args.compact) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
