# json-salvage

[![CI](https://github.com/rawqubit/json-salvage/actions/workflows/ci.yml/badge.svg)](https://github.com/rawqubit/json-salvage/actions/workflows/ci.yml) [![license](https://img.shields.io/badge/license-MIT-56d3a6)](https://github.com/rawqubit/json-salvage/blob/main/LICENSE) [![python](https://img.shields.io/badge/python-3.9%2B-3776ab)](https://github.com/rawqubit/json-salvage)

**Recover usable data from the JSON that language models actually emit.**

`json-salvage` takes the fenced, single-quoted, commented, truncated or
garbage-suffixed text a model returns and gives back the Python values inside
it. The repair is a character-level scanner with an explicit stack of open
containers, so a response cut off mid-object still comes back as a closed
object rather than an error.

## Before and after

| Broken output | Recovered |
| --- | --- |
| <code>```json<br>{"name": "Ada", "born": 1815}<br>```</code> | `{"name": "Ada", "born": 1815}` |
| `Here is the result:`<br>`{"ok": true}`<br>`Let me know if you need more.` | `{"ok": true}` |
| `{"tags": ["math", "logic",],}` | `{"tags": ["math", "logic"]}` |
| `{name: 'Ada', active: True}` | `{"name": "Ada", "active": true}` |
| `{"items": [1, 2, 3` | `{"items": [1, 2, 3]}` |
| `{"a": 1, "b` | `{"a": 1}` |

Every row above is asserted in `tests/test_json_salvage.py`, and
`examples/broken_outputs.txt` collects the same failures as full model-sized
responses.

## Install

One file, standard library only, Python 3.9 or newer. Copy `json_salvage.py`
into your project, or run it in place.

```
python3 json_salvage.py response.txt
```

That prints the first value it can recover, pretty-printed, and exits 0. When
there is nothing to recover it prints `json-salvage: no JSON value found` on
stderr and exits 1.

| Command | What it does |
| --- | --- |
| `python3 json_salvage.py FILE` | Pretty-printed first value |
| `python3 json_salvage.py -` | Read stdin instead of a file |
| `--all` | Every value, one per line, compact |
| `--compact` | Compact instead of pretty |
| `--stream` | Read stdin in 64-byte chunks, print each repaired snapshot |
| `--quiet` | No output, exit code only |

## Streaming

`iter_salvage` takes an iterable of string chunks, repairs the accumulated
buffer after every chunk, and yields the newest value only when it differs from
the last one it yielded. That is the whole loop a UI needs to render an object
while the model is still generating it:

```python
from json_salvage import iter_salvage

for value in iter_salvage(streamed_chunks):   # each chunk: str from the API
    render(value)                             # always valid, never repeats
```

The snapshots only grow: `{}` becomes `{"name": "Ada"}` becomes
`{"name": "Ada", "born": 1815}`. Every yielded value is complete and
serialisable, so a renderer never has to deal with a half-open brace, and no
yield serialises shorter than the one before it. When a half-written member is
dropped mid-stream, that snapshot is held back rather than rendered; when the
chunks run out, the value for the complete buffer is always yielded.

## API

```python
salvage(text: str) -> Any
```
The first recoverable JSON value. Raises `ValueError('no JSON value found')`
when nothing is recoverable. When prose comes first, a structured value
outranks a stray number or apostrophe in that prose, so
`"here are 3 fields: {...}"` returns the object, not `3`.

```python
salvage_all(text: str) -> list
```
Every recoverable value, in document order. Returns `[]` when there is none and
never raises, whatever the input.

```python
repair(text: str) -> str
```
Canonical JSON text for the first value. Raises `ValueError` when unrecoverable,
and is idempotent: `repair(repair(x)) == repair(x)`.

```python
iter_salvage(chunks) -> Iterator[Any]
```
Consume an iterable of string chunks and yield the newest repaired value after
each chunk, only when it differs from the last one yielded.

Values are built by parsing the repaired text with `json`, so key order is
preserved, integers stay integers and floats stay floats. `NaN`, `Infinity` and
`-Infinity` are kept as those tokens, which `json` reads back as the
corresponding floats.

## What it does not do

- It does not validate a schema. You get whatever the text contained.
- It does not fix semantically wrong data. A recovered `"age": "banana"` stays
  `"banana"`.
- It does not touch YAML. YAML is not JSON with different punctuation, and a
  scanner that pretends otherwise loses data.
- It will happily recover a value the model did not intend: a number in prose,
  a JSON snippet in an example inside an explanation, or the first of two
  answers. It reports what is in the text; deciding whether that is the value
  you wanted is your job.

## Tests

```
python3 -m unittest discover -s tests -v
```

The suite is table-driven and covers every repair in the table above plus
fencing without a language tag, comments, Python literals, smart quotes,
escaped strings, BOM handling, JSONL, nested truncation, a
fuzz loop over seeded random bytes, and the CLI.

## License

MIT, 2026 Srinikhil Chakilam.

Built by [Srinikhil Chakilam](https://srinikhilchakilam.com). If it rescues a production run of yours, I want to hear about it.
