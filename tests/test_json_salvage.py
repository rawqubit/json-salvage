"""Tests for json_salvage. Table-driven, standard library only."""

import json
import math
import random
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import json_salvage  # noqa: E402
from json_salvage import iter_salvage, repair, salvage, salvage_all  # noqa: E402

SCRIPT = ROOT / "json_salvage.py"
CORPUS = ROOT / "examples" / "broken_outputs.txt"


# --------------------------------------------------------------------------
# the broken input every claim in the README rests on
# --------------------------------------------------------------------------

REPAIRS = [
    (
        "markdown fences with a language tag",
        'Here you go:\n\n```json\n{"a": 1, "b": [1, 2]}\n```\n',
        {"a": 1, "b": [1, 2]},
    ),
    (
        "markdown fences without a language tag",
        "```\n{'a': 1}\n```",
        {"a": 1},
    ),
    (
        "prose before and after the value",
        'Here is the result:\n{"ok": true, "n": 2}\nLet me know if you need more.',
        {"ok": True, "n": 2},
    ),
    (
        "prose before a value that also contains a number",
        'I found 3 issues, here is the payload:\n{"issues": [1, 2, 3]}',
        {"issues": [1, 2, 3]},
    ),
    (
        "trailing commas in objects and arrays",
        '{"a": 1, "b": [2, 3, 4,], "c": {"d": 5,},}',
        {"a": 1, "b": [2, 3, 4], "c": {"d": 5}},
    ),
    (
        "single-quoted strings and keys",
        "{'name': 'Ada', 'language': 'math'}",
        {"name": "Ada", "language": "math"},
    ),
    (
        "unquoted javascript-style keys",
        "{a: 1, b: 'x', c: [true, false]}",
        {"a": 1, "b": "x", "c": [True, False]},
    ),
    (
        "python literals",
        '{"on": True, "off": False, "none": None}',
        {"on": True, "off": False, "none": None},
    ),
    (
        "missing colon between key and value",
        '{"a" 1, "b" 2}',
        {"a": 1, "b": 2},
    ),
    (
        "comments",
        '{\n  // why\n  "a": 1, /* inline */ "b": 2\n}',
        {"a": 1, "b": 2},
    ),
    (
        "truncated string",
        '{"name": "unterminated',
        {"name": "unterminated"},
    ),
    (
        "truncated array",
        '{"items": [1, 2, 3',
        {"items": [1, 2, 3]},
    ),
    (
        "truncated mid-key",
        '{"a": 1, "b',
        {"a": 1},
    ),
    (
        "truncated mid-value",
        '{"a": 1, "b": ',
        {"a": 1},
    ),
    (
        "truncated on a dangling comma",
        '{"a": 1, ',
        {"a": 1},
    ),
    (
        "trailing garbage after a complete value",
        '{"a": 1} extra words here',
        {"a": 1},
    ),
    (
        "escaped quotes and backslashes",
        '{"path": "C:\\\\Users\\\\ada", "quote": "she said \\"hi\\" and left"}',
        {"path": "C:\\Users\\ada", "quote": 'she said "hi" and left'},
    ),
    (
        "smart quotes as delimiters",
        '{“name”: “Ada”, “tags”: [‘math’, ‘logic’]}',
        {"name": "Ada", "tags": ["math", "logic"]},
    ),
    (
        "bare literals at top level",
        "true",
        True,
    ),
    (
        "bare null at top level",
        "null",
        None,
    ),
    (
        "bare number at top level",
        "42",
        42,
    ),
    (
        "unterminated fenced block",
        "```json\n{\"draft\": true, \"steps\": [\"one\", \"two\"]",
        {"draft": True, "steps": ["one", "two"]},
    ),
    (
        "bom and leading whitespace",
        "\ufeff\n\t  {\"bom\": true}",
        {"bom": True},
    ),
    (
        "deeply nested object truncated at depth six",
        '{"l1": {"l2": {"l3": {"l4": {"l5": {"l6": [1, 2, 3',
        {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": [1, 2, 3]}}}}}},
    ),
    (
        "truncated at the very first character",
        "{",
        {},
    ),
    (
        "array truncated after a nested object",
        '[{"id": 1}, {"id": 2',
        [{"id": 1}, {"id": 2}],
    ),
]

SPECIAL_NUMBERS = '{"loss": NaN, "score": Infinity, "floor": -Infinity}'


class TestRepairs(unittest.TestCase):
    def test_table(self):
        for name, broken, expected in REPAIRS:
            with self.subTest(name):
                self.assertEqual(salvage(broken), expected)
                self.assertEqual(json.loads(repair(broken)), expected)

    def test_nan_and_infinity(self):
        value = salvage(SPECIAL_NUMBERS)
        self.assertTrue(math.isnan(value["loss"]))
        self.assertEqual(value["score"], float("inf"))
        self.assertEqual(value["floor"], float("-inf"))
        self.assertEqual(
            repair(SPECIAL_NUMBERS),
            '{"loss": NaN, "score": Infinity, "floor": -Infinity}',
        )

    def test_numbers_keep_their_type(self):
        value = salvage('{"count": 7, "ratio": 1.5, "big": 1e6, "neg": -12}')
        self.assertIs(type(value["count"]), int)
        self.assertIs(type(value["ratio"]), float)
        self.assertIs(type(value["neg"]), int)
        self.assertEqual(value["ratio"], 1.5)
        self.assertEqual(value["big"], 1000000.0)
        # text form is preserved rather than re-rendered by the float repr
        self.assertEqual(repair('{"scale": 1e6}'), '{"scale": 1e6}')
        self.assertEqual(repair('{"ratio": 1.5}'), '{"ratio": 1.5}')

    def test_key_order_is_preserved(self):
        broken = '{"z": 1, "m": 2, "a": 3, "b": 4}'
        self.assertEqual(list(salvage(broken)), ["z", "m", "a", "b"])
        self.assertEqual(repair(broken), '{"z": 1, "m": 2, "a": 3, "b": 4}')

    def test_output_is_deterministic(self):
        broken = "{b: 'x', a: 1, c: [1, 2,], d: None}"
        results = {repair(broken) for _ in range(20)}
        self.assertEqual(len(results), 1)


class TestSalvageAll(unittest.TestCase):
    def test_concatenated_values(self):
        self.assertEqual(salvage_all('{"a": 1}{"b": 2}'), [{"a": 1}, {"b": 2}])

    def test_newline_separated_jsonl(self):
        text = '{"id": 1}\n{"id": 2}\n{"id": 3}\n'
        self.assertEqual(salvage_all(text), [{"id": 1}, {"id": 2}, {"id": 3}])

    def test_scalars_and_containers_together(self):
        self.assertEqual(salvage_all("1 true null"), [1, True, None])

    def test_nothing_recoverable(self):
        self.assertEqual(salvage_all(""), [])
        self.assertEqual(salvage_all("just prose, nothing else"), [])
        self.assertEqual(salvage_all("/* only a comment */"), [])

    def test_never_raises_on_odd_input(self):
        for text in ["", "\x00\x01\x02", "{{{{", "[[[[", "}}}}", "\\\\", '"', "'",
                     "{\"a\": " * 50, "/*", "//", "\ufeff", "\ud800"]:
            self.assertEqual(salvage_all(text), salvage_all(text))

    def test_megabyte_of_garbage(self):
        text = "lorem ipsum dolor sit amet consectetur " * 30000
        self.assertGreater(len(text), 1000000)
        start = time.monotonic()
        self.assertEqual(salvage_all(text), [])
        self.assertLess(time.monotonic() - start, 30.0)

    def test_deep_bracket_run_does_not_recurse(self):
        value = salvage("[" * 200 + "]" * 200)
        depth = 0
        node = value
        while isinstance(node, list):
            depth += 1
            node = node[0] if node else None
        self.assertEqual(depth, 200)


class TestErrors(unittest.TestCase):
    def test_salvage_raises_exact_message(self):
        with self.assertRaises(ValueError) as caught:
            salvage("nothing to see here")
        self.assertEqual(str(caught.exception), "no JSON value found")

    def test_repair_raises_exact_message(self):
        with self.assertRaises(ValueError) as caught:
            repair("   ")
        self.assertEqual(str(caught.exception), "no JSON value found")

    def test_empty_input(self):
        with self.assertRaises(ValueError):
            salvage("")


class TestIdempotentRepair(unittest.TestCase):
    def test_every_case_in_the_repair_table(self):
        for name, broken, _expected in REPAIRS:
            with self.subTest(name):
                once = repair(broken)
                self.assertEqual(repair(once), once)

    def test_special_numbers(self):
        once = repair(SPECIAL_NUMBERS)
        self.assertEqual(repair(once), once)

    def test_whole_corpus_file(self):
        once = repair(CORPUS.read_text(encoding="utf-8"))
        self.assertEqual(repair(once), once)


def read_corpus():
    sections = {}
    name = None
    body = []
    for line in CORPUS.read_text(encoding="utf-8").splitlines(True):
        if line.startswith("### "):
            if name is not None:
                sections[name] = "".join(body).strip("\n")
            name = line[4:].strip()
            body = []
        elif name is not None:
            body.append(line)
    if name is not None:
        sections[name] = "".join(body).strip("\n")
    return sections


CORPUS_EXPECTED = {
    "markdown-fence": {"name": "Ada Lovelace", "born": 1815,
                       "tags": ["math", "computing"]},
    "prose-around": {"ok": True, "items": [1, 2, 3], "note": "all good"},
    "trailing-commas": {"a": 1, "b": [2, 3, 4], "c": {"d": 5}},
    "single-quotes": {"name": "Ada", "language": "Python's rival"},
    "unquoted-keys": {"a": 1, "b": "x", "c": [True, False]},
    "python-literals": {"enabled": True, "missing": None, "checked": False},
    "numeric-specials": {"score": float("inf"), "floor": float("-inf")},
    "comments": {"retries": 3, "timeout": 1.5},
    "truncated-string": {"name": "unterminated"},
    "truncated-array": {"items": [1, 2, 3]},
    "truncated-key": {"a": 1},
    "trailing-garbage": {"status": "ok"},
    "concatenated-values": {"a": 1},
    "newline-jsonl": {"id": 1},
    "escaped-strings": {"path": "C:\\Users\\ada",
                        "quote": 'she said "hello" and left'},
    "smart-quotes": {"name": "Ada", "tags": ["math", "logic"]},
    "bare-scalars": True,
    "number-types": {"count": 7, "ratio": 1.5, "big": 1000000.0, "negative": -12},
    "fenced-unterminated": {"draft": True, "steps": ["one", "two"]},
    "bom-whitespace": {"bom": True},
    "deep-nesting": {"l1": {"l2": {"l3": {"l4": {"l5": {"l6": [1, 2, 3]}}}}}},
}


class TestCorpus(unittest.TestCase):
    def test_corpus_covers_every_named_case(self):
        self.assertEqual(set(read_corpus()), set(CORPUS_EXPECTED))

    def test_every_case_recovers_its_value(self):
        for name, text in read_corpus().items():
            with self.subTest(name):
                value = salvage(text)
                expected = CORPUS_EXPECTED[name]
                if name == "numeric-specials":
                    self.assertTrue(math.isnan(value["loss"]))
                    self.assertEqual(value["score"], expected["score"])
                    self.assertEqual(value["floor"], expected["floor"])
                else:
                    self.assertEqual(value, expected)

    def test_every_case_repairs_idempotently(self):
        for name, text in read_corpus().items():
            with self.subTest(name):
                once = repair(text)
                self.assertEqual(repair(once), once)

    def test_whole_file_yields_one_value_per_payload(self):
        values = salvage_all(CORPUS.read_text(encoding="utf-8"))
        self.assertEqual(len(values), 26)


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

FULL_OBJECT = {
    "model": "gpt-4o",
    "usage": {"prompt": 12, "completion": 34},
    "items": [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ],
}


def chunked(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


class TestStreaming(unittest.TestCase):
    def test_seven_character_chunks(self):
        pretty = json.dumps(FULL_OBJECT, indent=2)
        chunks = chunked(pretty, 7)
        yields = list(iter_salvage(chunks))

        self.assertEqual(yields[-1], FULL_OBJECT)
        self.assertGreaterEqual(len(yields), 3)
        self.assertLess(len(yields), len(chunks))

        for value in yields:
            json.dumps(value)  # every yield is serialisable, hence valid

        sizes = [len(json.dumps(v)) for v in yields]
        self.assertEqual(sizes, sorted(set(sizes)))

    def test_snapshots_the_readme_shows(self):
        text = '{"name": "Ada", "born": 1815}'
        chunks = [text[i:i + 7] for i in range(0, len(text), 7)]
        self.assertEqual(
            list(iter_salvage(chunks)),
            [{}, {"name": "Ada"}, {"name": "Ada", "born": 1815}],
        )

    def test_never_yields_an_invalid_or_repeated_value(self):
        pretty = json.dumps(FULL_OBJECT, indent=2)
        seen = []
        for value in iter_salvage(chunked(pretty, 1)):
            json.dumps(value)
            seen.append(json.dumps(value))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(json.loads(seen[-1]), FULL_OBJECT)

    def test_nothing_yielded_before_a_value_exists(self):
        self.assertEqual(list(iter_salvage(["   ", "\n", "no json"])), [])
        self.assertEqual(list(iter_salvage([])), [])

    def test_a_string_value_streams_growth(self):
        chunks = ['"', "hel", "lo", " world", '"']
        values = list(iter_salvage(chunks))
        self.assertEqual(values[-1], "hello world")
        self.assertTrue(all(isinstance(v, str) for v in values))

    def test_monotonic_over_every_corpus_case(self):
        for name, text in read_corpus().items():
            for size in (1, 2, 3, 5, 7):
                with self.subTest("%s/%d" % (name, size)):
                    chunks = [text[i:i + size] for i in range(0, len(text), size)]
                    values = list(iter_salvage(chunks))
                    if not values:
                        continue
                    sizes = [len(json.dumps(v)) for v in values]
                    body = sizes[:-1]
                    self.assertEqual(body, sorted(set(body)))
                    self.assertEqual(values[-1], salvage(text))

    def test_truncated_stream_keeps_growing(self):
        chunks = ['{"items": [1, ', "2, 3", "], \"done\": tr", "ue}"]
        values = list(iter_salvage(chunks))
        self.assertEqual(values[-1], {"items": [1, 2, 3], "done": True})
        sizes = [len(json.dumps(v)) for v in values]
        self.assertEqual(sizes, sorted(sizes))


# --------------------------------------------------------------------------
# fuzz
# --------------------------------------------------------------------------

class TestFuzz(unittest.TestCase):
    def test_three_hundred_random_byte_strings(self):
        rng = random.Random(20260119)
        start = time.monotonic()
        for _ in range(300):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 400)))
            text = data.decode("utf-8", "replace")
            values = salvage_all(text)
            self.assertIsInstance(values, list)
            try:
                salvage(text)
            except ValueError as exc:
                self.assertEqual(str(exc), "no JSON value found")
            try:
                repaired = repair(text)
            except ValueError:
                pass
            else:
                json.loads(repaired)  # a repair is always valid JSON
            list(iter_salvage(chunked(text, 16)))
        self.assertLess(time.monotonic() - start, 60.0)

    def test_random_json_ish_fragments(self):
        rng = random.Random(7)
        alphabet = '{}[]",:\'`\\ \n\tabc123-.truefalsnul'
        for _ in range(300):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 120)))
            self.assertIsInstance(salvage_all(text), list)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def run_cli(args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestCli(unittest.TestCase):
    def test_file_pretty(self):
        result = run_cli([str(CORPUS)])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout),
                         {"name": "Ada Lovelace", "born": 1815,
                          "tags": ["math", "computing"]})
        self.assertIn('\n  "born": 1815', result.stdout)

    def test_stdin_dash(self):
        result = run_cli(["-"], stdin='{"a": 1, "b": [1, 2,],}')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {"a": 1, "b": [1, 2]})

    def test_all_and_compact(self):
        result = run_cli(["-", "--all"], stdin='{"a": 1}{"b": 2}')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ['{"a":1}', '{"b":2}'])

    def test_stream(self):
        pretty = json.dumps(FULL_OBJECT, indent=2)
        result = run_cli(["-", "--stream"], stdin=pretty)
        self.assertEqual(result.returncode, 0)
        lines = result.stdout.splitlines()
        self.assertGreaterEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[-1]), FULL_OBJECT)

    def test_quiet_reports_only_the_exit_code(self):
        ok = run_cli(["-", "--quiet"], stdin='{"a": 1}')
        self.assertEqual((ok.returncode, ok.stdout, ok.stderr), (0, "", ""))
        bad = run_cli(["-", "--quiet"], stdin="nothing here")
        self.assertEqual((bad.returncode, bad.stdout, bad.stderr), (1, "", ""))

    def test_unrecoverable_input(self):
        result = run_cli(["-"], stdin="nothing here")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "json-salvage: no JSON value found")
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_file_does_not_traceback(self):
        result = run_cli([str(ROOT / "does-not-exist.txt")])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertTrue(result.stderr.startswith("json-salvage: "))

    def test_stream_on_unrecoverable_input(self):
        result = run_cli(["-", "--stream"], stdin="nothing here")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)


class TestModuleSurface(unittest.TestCase):
    def test_exports(self):
        self.assertEqual(sorted(json_salvage.__all__),
                         ["iter_salvage", "repair", "salvage", "salvage_all"])

    def test_accepts_bytes(self):
        self.assertEqual(salvage(b'{"a": 1}'), {"a": 1})
        self.assertEqual(salvage_all(b'{"a": 1}'), [{"a": 1}])
        self.assertEqual(repair(bytearray(b'{"a": 1}')), '{"a": 1}')


if __name__ == "__main__":
    unittest.main()
