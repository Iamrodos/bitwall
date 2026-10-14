"""Golden cases for the Rust protocol module, written by bitwall/protocol.py.

`python3 bitwall/rust/tests/golden/protocol.py` writes protocol.json beside
it: every input with what Python made of it, so `cargo test` holds the
Rust port to the same answers without needing Python.
"""
import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3]))
from bitwall import protocol  # noqa: E402

M = protocol.MAX_MESSAGE
rng = random.Random(1)


def message(m):
    if m is None:
        return None
    return {"name": m.name, "args": list(m.args),
            "opts": [[k, v] for k, v in m.opts.items()]}


def fallible(f, *a, **k):
    try:
        return {"ok": f(*a, **k)}
    except ValueError:
        return {"err": True}


# Everything Python calls whitespace, which `\s`, strip and split all use.
spaces = [c for c in range(0x110000) if chr(c).isspace()]

alphabet = ["a", "b", "=", "%", " ", "\t", "\n", "\r", "\x00", "\x1b", "\x1c", "\x1f",
            "\x7f", "\x85", "\xa0", " ", "　", "​", "é", "😀", "-", "1",
            "%41", "%2", "%zz", "%E2%82%AC", "%ff", ",", "+", "U", "u"]


def noise(n):
    return "".join(rng.choice(alphabet) for _ in range(n))


parse_in = [
    "", " ", "\n", "hello v1 line", "  area 174x42 14  \n", "area\t174x42\x0b14",
    "opened sysinfo-4821", "input key key=Return mods=ctrl", "input tap cell=3,4 area=174,42",
    "screen colour=true rate=30 pixels=1920x1080 cell=12x28 glyphs=U+2500-259F,U+2800-28FF",
    "error stats at=zz: unknown point id=7", "label Back%20room", "x title=my%20value",
    "x 50%", "x %41%E2", "x %E2%82%AC%41", "x %e2%82%ac", "x %C0%80", "x %ED%A0%80",
    "x %%41", "x %4", "x a=%3D", "x a=b=c", "x =b", "x a=", "x =", "=x", "a=b",
    "x a=1 a=2 b=3 a=4", "x b=1 a=2", "x\x00y", "x y\x01", "x \x7f", "x\x1cy", "x\x1fy z",
    "x\x85y", "x\xa0y", "x y", "x　y", "x​y", "é 😀 k=ü", "\x1b_bitwall x",
    "x " + "a" * 256, "x " + "a" * 257, "a" * 256, "a" * 257, "x k=" + "v" * 256,
    "x k=" + "v" * 257, "x " + " ".join(["a"] * 63), "x " + " ".join(["a"] * 64),
    "x" * M, "x" * (M + 1), " " * 10 + "x" * M + " " * 10, "x " + "é" * 256,
    "x " + "é" * 2047, "x " + "é" * 2048,
    "watching ²", "watching 12", "hello V1", "x ﻿", "﻿x",
]
parse_in += [noise(rng.randint(0, 12)) for _ in range(400)]
parse_out = [message(protocol.parse(s)) for s in parse_in]

number_in = ["0", "-0", "12", "-12", "+1", " 1", "1 ", "1\n", "\n1", "1\n\n", "²", "١",
             "", "-", "1.0", "1e3", "9" * 18, "9" * 19, "9" * 20, "-" + "9" * 19,
             "9223372036854775807", "9223372036854775808", "-9223372036854775808",
             "-9223372036854775809", "007", "0x10"]
number_out = [protocol.number(s) for s in number_in]

int_cases = []
for line, key, low, high in [("x n=5", "n", 0, 10), ("x n=11", "n", 0, 10),
                             ("x n=-1", "n", 0, 10), ("x n=a", "n", 0, 10),
                             ("x m=5", "n", 0, 10), ("x n=0 n=7", "n", 0, 7),
                             ("x n=99999999999999999999", "n", 0, 10)]:
    int_cases.append({"line": line, "key": key, "low": low, "high": high,
                      "out": protocol.parse(line).int(key, low, high)})

word_in = ["", "plain", "my value", "a=b", "50%", "tab\there", "nl\n", "\x00\x1f\x7f",
           "\x85\xa0 　", "é😀", "%41", "\x1c\x1d\x1e\x1f", "​"] + \
          [noise(rng.randint(0, 10)) for _ in range(200)]
word_cases = [{"in": s, "word": protocol.word(s),
               "back": protocol.unword(protocol.word(s))} for s in word_in]
unword_in = ["", "abc", "%", "50%", "%41", "%4", "%4g", "%41%42", "%e2%82%ac", "%E2%82%AC",
             "%E2%82", "%41%E2", "%E2%41", "%%41%", "a%20b%20c", "%C0%80", "%ED%A0%80",
             "%F4%90%80%80", "%00", "%0A", "é%41"] + \
            [noise(rng.randint(0, 10)) for _ in range(200)]
unword_out = [protocol.unword(s) for s in unword_in]

render_in = [
    ("hello", ["v1", "line"], {}), ("font", ["14"], {}),
    ("stats", ["1"], {"show": "fps,in", "at": "ne", "size": 14}),
    ("meta", [], {"label": "Galaga", "description": "a game with spaces=and %"}),
    ("x", [""], {}), ("", [], {}), ("a b", [], {}), ("a=b", [], {}), ("ping\n", [], {}),
    ("ping\n\n", [], {}), ("\nping", [], {}), ("x", [], {"k\n": "v"}), ("x", [], {"k": ""}),
    ("x", [], {"": "v"}), ("x", ["a" * 256], {}), ("x", ["a" * 257], {}),
    ("x", [" " * 85], {}), ("x", [" " * 86], {}), ("x", ["a"] * 63, {}),
    ("x", ["a"] * 64, {}), ("x", ["a" * 250] * 17, {}), ("x", ["a" * 255] * 16, {}),
    ("x", ["a" * 256] * 16, {}), ("x", ["é" * 256], {}), ("é", ["😀"], {"ü": "ß"}),
    ("x", [], {"b": "1", "a": "2", "B": "3", "é": "4", "_": "5", "😀": "6", "￿": "7"}),
    ("x\x7f", [], {}), ("x", ["\x00"], {}), ("x", [], {"k": "\x1b"}),
    ("x" * 256, [], {}), ("x" * 257, [], {}), ("x" * 256 + "\n", [], {}),
    ("x", [noise(5) for _ in range(5)], {"k": noise(8)}),
]
render_cases = [{"name": n, "args": a, "opts": [[k, str(v)] for k, v in o.items()],
                 "render": fallible(protocol.render, n, *a, **o),
                 "command": fallible(protocol.command, n, *a, **o)}
                for n, a, o in render_in]

# Lines: streams cut into pieces at random, one of them with no newlines.
streams = ["hello v1 line\nopened s-1\narea 174x42 14\n\nbad =x\nwatching 1\npartial",
           "a\r\nb\n", "é😀 k=v\n" * 20, ("x" * 5000 + "\n") * 3 + "tail",
           "y" * (M * 16 + 10) + "\nafter 1\n", "z" * (M * 20) + "\n",
           "é" * (M * 17) + "\nlast é\n", "\n\n\n", "one two\nthree\x85\n"]
lines_cases = []
for s in streams:
    for _ in range(4 if len(s) < 10000 else 2):
        cuts = sorted(rng.sample(range(len(s) + 1), min(len(s) + 1, rng.randint(0, 12))))
        pieces = [s[a:b] for a, b in zip([0] + cuts, cuts + [len(s)])]
        real = protocol.Lines()
        said = [message(m) for p in pieces for m in real.feed(p)]
        keep, protocol.parse = protocol.parse, lambda line: line
        try:
            raw = protocol.Lines()
            lines = [line for p in pieces for line in raw.feed(p)]
        finally:
            protocol.parse = keep
        lines_cases.append({"pieces": pieces, "lines": lines, "messages": said})

ranges_in = ["", "U+2588", "u+2500-257f", "U+2500-259F,U+2800-28FF", " U+41 , U+42-43 ",
             "U+43-42", "U+110000", "U+10FFFF", "U+0-10FFFF", "U+1234567", "U+", "2588",
             "U+zz", "U+41,junk,U+42", ",".join(["U+41"] * 40),
             ",".join(["x"] * 31 + ["U+41"]), ",".join(["x"] * 32 + ["U+41"]), "U+41-",
             "U+41-42-43", "U+41\n", "U+٤١", "　U+41\x1c", "U+41\x1c,U+42​"]
ranges_cases = [{"in": s, "out": [list(r) for r in protocol.ranges(s)]} for s in ranges_in]
covers_cases = []
for spans, text in [([], "anything"), ([[0x41, 0x5A]], "ABC"), ([[0x41, 0x5A]], "ABc"),
                    ([[0x41, 0x5A]], ""), ([[0x2500, 0x257F], [0x20, 0x7E]], "a─b"),
                    ([[0x1F600, 0x1F600]], "😀"), ([[0x1F600, 0x1F600]], "😀😁")]:
    covers_cases.append({"spans": spans, "text": text,
                         "out": protocol.covers([tuple(x) for x in spans], text)})

version_in = ["hello v1 line", "hello V2", "hello 3", "hello", "hello vx", "hullo v1",
              "hello v²", "hello -4", "hello v-4"]
version_cases = [{"in": s, "out": protocol.version_of(protocol.parse(s))} for s in version_in]

qoi = (b"qoif" + (3).to_bytes(4, "big") + (2).to_bytes(4, "big") + b"\x04\x00"
       + b"\x00" * 7 + b"\x01")
png = (b"\x89PNG\r\n\x1a\n" + b"\0\0\0\x0dIHDR" + (640).to_bytes(4, "big")
       + (480).to_bytes(4, "big") + b"\x08\x02\0\0\0" + b"\0" * 16
       + b"\0\0\0\0IEND\xaeB`\x82")


def sof(marker, w, h, comps):
    return (b"\xff" + bytes([marker]) + (8 + 3 * comps).to_bytes(2, "big") + b"\x08"
            + h.to_bytes(2, "big") + w.to_bytes(2, "big") + bytes([comps])
            + b"\x11\x00\x00" * comps)


app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
jpegs = [b"\xff\xd8" + app0 + sof(0xC0, 800, 600, 3) + b"\xff\xd9",
         b"\xff\xd8" + sof(0xC2, 10, 20, 1) + b"\xff\xd9",
         b"\xff\xd8" + sof(0xC0, 10, 20, 4) + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\xff" + sof(0xC1, 1, 1, 3) + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\xd0" + sof(0xC0, 5, 6, 3) + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\x01" + sof(0xC0, 5, 6, 3) + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\xc4\x00\x04\x00\x00" + sof(0xC0, 7, 8, 3) + b"\xff\xd9",
         b"\xff\xd8" + sof(0xC0, 0, 8, 3) + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\xda\x00\x08" + b"\xff\xd9", b"\xff\xd8\xff\xd9",
         b"\xff\xd8" + b"\xff\xe0\x00\x01" + b"\xff\xd9", b"\xff\xd8\x00\x00\xff\xd9",
         b"\xff\xd8" + b"\xff\xc0\x00\x11\x08" + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\xc0\x00\x07\x08\x00\x01\x00\x01\x03" + b"\xff\xd9",
         b"\xff\xd8" + b"\xff\xe0\xff\xff" + b"\xff\xd9"]
blobs = [b"", qoi, qoi[:-1], qoi[:21], png, png[:-1], png[:44], b"junk" * 12,
         b"qoif", b"\x89PNG\r\n\x1a", b"\xff\xd8\xff"] + jpegs
for _ in range(80):
    base = bytearray(rng.choice([qoi, png] + jpegs))
    for _ in range(rng.randint(1, 4)):
        base[rng.randrange(len(base))] = rng.randrange(256)
    blobs.append(bytes(base))
images = []
for b in blobs:
    images.append({"hex": b.hex(), "of": protocol.image_of(b),
                   "size": {c: (list(s) if (s := protocol.image_size(c, b)) else None)
                            for c in ("qoi", "png", "jpeg", "gif")}})

out = {
    "constants": {"APC": protocol.APC, "ST": protocol.ST, "MARK": protocol.MARK,
                  "VERSION": protocol.VERSION, "GRAMMAR": protocol.GRAMMAR,
                  "TAG": protocol.TAG, "MAX_MESSAGE": M, "MAX_TOKENS": protocol.MAX_TOKENS,
                  "MAX_RANGES": protocol.MAX_RANGES, "hello": protocol.hello(),
                  "announce": protocol.announce("area", "174x42", "14"),
                  "IMAGES": list(protocol.IMAGES)},
    "spaces": spaces,
    "parse": [{"in": i, "out": o} for i, o in zip(parse_in, parse_out)],
    "number": [{"in": i, "out": o} for i, o in zip(number_in, number_out)],
    "int": int_cases,
    "word": word_cases,
    "unword": [{"in": i, "out": o} for i, o in zip(unword_in, unword_out)],
    "render": render_cases,
    "lines": lines_cases,
    "ranges": ranges_cases,
    "covers": covers_cases,
    "version_of": version_cases,
    "images": images,
}
(HERE / "protocol.json").write_text(json.dumps(out, ensure_ascii=False) + "\n")
total = sum(len(v) for k, v in out.items() if isinstance(v, list) and k != "spaces")
print(f"{total} cases written to {HERE / 'protocol.json'}")
