#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow"]
# ///
"""One wall cut into panes, each a wall of its own.

    senders/panes/main.py localhost:kitchen --name snake
    bitwall-ctl localhost:kitchen-snake split down sysinfo  # snake over sysinfo
    senders/... localhost:kitchen-snake  senders/... localhost:kitchen-sysinfo

A proxy wall: a sender outward and a receiver inward. Outward it is one
sender on the real wall's base, as any other; the real wall never learns
what a pane is. Inward each pane is a wall on this host,
`<run>/bitwall/<wall>-<pane>/`, made by the receiver core
(`bitwall/receivers/core.py`) with a rectangle of the real wall for its
glass, so a sender attaches to a pane as to any wall and cannot tell it
from a whole one. Every frame the panes' cells are composed into one grid
at their rectangles, and bitwall's diffing sends what changed.

The panes are the leaves of a tree, as tmux and i3 keep theirs: a cut is
a direction and the share of its first side. A pane is cut by asking it,
on a controller session, as `split-window` targets a pane:

    split right|down|left|up <name> [share]   a new pane beside this one
    unsplit                                   this pane goes, its space to its sibling
    share <percent>                           this pane's share of its cut
    layout                                    every pane, a line each

A pane's name is its own for life, whatever is cut around it, and the
tree is kept in ~/.bitwall/panes/, so a restart brings the same panes
back under the same names and their senders find their walls again.

What a pane says of its glass is the real wall's: its glyphs, its depth,
its cells, whether it takes kitty pictures and touch, and whether anybody
is watching. Its grid is fixed at its rectangle, so a font asked of a pane
is answered with the area it has: the font is the real wall's, `--font`.
A pane's kitty pictures are placed on the real wall at the pane's offset,
cropped to it, under ids of the pane's own; a tap goes to the pane it
falls in. A pane's `picture glass` is the real wall's own picture of its
glass, asked over a controller session of the split's, cropped to the
pane: exact, pictures and all, where the real wall offers pictures, and
not offered where it does not. Video layers are not offered.
"""
import argparse
import base64
import json
import os
import re
import signal
import sys
import threading
import time

BITWALL = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, BITWALL)

import bitwall                                                  # noqa: E402
from bitwall import grid as gridmod                             # noqa: E402
from bitwall import letters, protocol                           # noqa: E402
from bitwall.receivers import core                              # noqa: E402

FONT = 14
#: A pane's name: a wall name's own rule, short enough to follow the real
#: wall's name and a dash and still be a wall name.
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
#: The least a pane may be, in cells, for a split to be taken.
LEAST = (4, 2)
#: Kitty ids on the real wall: each pane a range of its own, so two
#: senders' `i=1` never meet.
KITTY_BASE = 0x7A000000
KEEP = os.path.expanduser("~/.bitwall/panes")
DIM = "5a6472"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ the tree

class Leaf:
    def __init__(self, name):
        self.name = name

    def json(self):
        return {"pane": self.name}


class Cut:
    """Two sides, `h` side by side or `v` one over the other; `share` is
    the first side's per cent."""

    def __init__(self, way, share, a, b):
        self.way, self.share, self.a, self.b = way, share, a, b

    def json(self):
        return {"way": self.way, "share": self.share,
                "a": self.a.json(), "b": self.b.json()}


def from_json(d):
    if "pane" in d:
        return Leaf(d["pane"])
    return Cut(d["way"], d["share"], from_json(d["a"]), from_json(d["b"]))


def leaves(node):
    return [node] if isinstance(node, Leaf) else leaves(node.a) + leaves(node.b)


def parent_of(node, leaf, up=None):
    if node is leaf:
        return up
    if isinstance(node, Cut):
        return parent_of(node.a, leaf, node) or parent_of(node.b, leaf, node)
    return None


def lay(node, x, y, w, h, out):
    """Each pane's rectangle, (x, y, w, h) in cells, into `out`."""
    if isinstance(node, Leaf):
        out[node.name] = (x, y, w, h)
        return out
    if node.way == "h":
        first = min(max(1, round(w * node.share / 100)), max(1, w - 1))
        lay(node.a, x, y, first, h, out)
        lay(node.b, x + first, y, w - first, h, out)
    else:
        first = min(max(1, round(h * node.share / 100)), max(1, h - 1))
        lay(node.a, x, y, w, first, out)
        lay(node.b, x, y + first, w, h - first, out)
    return out


# ----------------------------------------------------------------- a pane

class Glass:
    """A pane's glass, as the receiver core reaches it: the cells it is
    told of are read back from its grid when composing, so only the
    pictures need anything done."""

    def __init__(self, panes, name):
        self.panes, self.name = panes, name

    def cells(self, data):
        self.panes.dirty = True

    def frame(self, z, head, data):
        pass

    def clear(self, z):
        pass

    def kitty(self, head, data):
        self.panes.kitty(self.name, head, data)

    def changed(self):
        self.panes.dirty = True

    def stats(self):
        pass

    def look(self, w, h, done):
        return self.panes.look(self.name, w, h, done)


class Panes:
    def __init__(self, target, first, font, run=None, here=None):
        self.target = target
        #: Where the real wall is, as `attach` takes it, and the run
        #: directory the panes are made in: this host's, unless told.
        self.run = run
        self.here = here or os.environ.get("XDG_RUNTIME_DIR") \
            or os.path.expanduser("~/.bitwall/run")
        self.host, _, self.wall_name = target.partition(":")
        self.wall_name = self.wall_name or "0"
        self.font = font
        self.lock = threading.Lock()
        self.keep = os.path.join(os.environ.get("BITWALL_PANES_KEEP", KEEP),
                                 re.sub(r"[^A-Za-z0-9_.-]", "_", target) + ".json")
        self.tree = self.load() or Leaf(first)
        self.walls = {}             # pane name -> core.Wall
        self.rects = {}             # pane name -> (x, y, w, h)
        self.area = None
        self.w = None
        self.dirty = True
        self.changed_tree = True
        self.resend = False
        # Kitty: per pane, its images {key: (f, w, h, data)} and its
        # placements as the core lists them; what is placed on the real
        # wall, as ids, so it can be taken down.
        self.images, self.places, self.placed = {}, {}, {}
        self.slots = {}
        #: A controller session on the real wall, for its picture of the
        #: glass, opened when first wanted; one look at a time through it.
        self.ctl = None
        self.ctl_lock = threading.Lock()

    # -- keeping the tree

    def load(self):
        try:
            with open(self.keep) as fh:
                return from_json(json.load(fh)["tree"])
        except (OSError, ValueError, KeyError):
            return None

    def save(self):
        os.makedirs(os.path.dirname(self.keep), exist_ok=True)
        tmp = self.keep + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"target": self.target, "tree": self.tree.json()}, fh, indent=1)
        os.replace(tmp, self.keep)

    def wall_of(self, pane):
        return f"{self.wall_name}-{pane}"

    # -- the real wall

    def attach(self):
        kw = {"run": self.run} if self.run else {}
        self.w = bitwall.attach(self.target, min_frame=1 / 20, **kw)
        with self.w.transition():
            self.w.set_font_size(self.font)
            self.w.set_source(self.render)
        self.w.before_leaving(self.take_down_pictures)
        # A real wall that comes back may hold none of the pictures: every
        # pane's are placed again with the first frame after.
        self.w.when_lost(lambda: setattr(self, "resend", True))
        if self.w.capabilities().get("touch"):
            self.w.want(touch=True)
        return self.w

    def screen(self):
        """What a pane says of its glass: the real wall's."""
        w = self.w
        spans = w.glyphs
        glyphs = ",".join(f"U+{lo:04X}" + (f"-{hi:04X}" if hi != lo else "")
                          for lo, hi in spans) if spans else None
        px = letters.resolution(w) or core.nominal_cell(self.font)
        return {"colour": getattr(w, "depth", None) or "true", "glyphs": glyphs,
                "kitty": w.offers("kitty"), "cell": (int(px[0]), int(px[1]))}

    # -- panes as walls

    def make(self, name):
        sc = self.screen()
        glass = Glass(self, name)
        wall = core.Wall(self.here, self.wall_of(name), glass, font=self.real_font(),
                         receiver="receiver panes", colour=sc["colour"],
                         glyphs=sc["glyphs"], layers=1, types=("cells",),
                         kitty=sc["kitty"], commands=self.commands_for(name))
        wall.start()
        # Watched as the real wall is, from the start: a core wall's glass
        # has nobody looking until told, and its sender would draw nothing.
        wall.set_visible(0 if self.w.watching() == 0 else 1)
        if self.w.capabilities().get("touch"):
            wall.set_accepts(("touch",))
        self.walls[name] = wall
        log(f"pane {name} is {self.host}:{self.wall_of(name)}")

    def real_font(self):
        """The font the real wall says it drew at, which a page's zoom may
        make other than the one asked for; the one asked for until it says."""
        said = self.w.font_size if self.w is not None else None
        return said or self.font

    def box(self, w, h):
        """A pane's glass: fixed at its grid, and a cell's size at every
        font scaled from the real wall's cell, so the largest font whose
        cells cover the grid, the font said in `area`, is the real one."""
        cw, ch = self.screen()["cell"]
        f0 = self.real_font()
        cells = []
        for f in range(core.FONT_LOW, core.FONT_HIGH + 1):
            if f > f0:
                cells.append([-(-cw * f // f0), -(-ch * f // f0)])
            else:
                cells.append([max(1, cw * f // f0), max(1, ch * f // f0)])
        return {"w": w * cw, "h": h * ch, "cells": cells, "fixed": [w, h],
                "font": f0}

    def relayout(self, cols, rows):
        """The tree laid over the real wall's area: panes made or ended to
        match it, and each told its rectangle where that changed."""
        with self.lock:
            rects = lay(self.tree, 0, 0, cols, rows, {})
            names = set(rects)
        for name in list(self.walls):
            if name not in names:
                wall = self.walls.pop(name)
                self.unplace(name)
                self.images.pop(name, None)
                self.places.pop(name, None)
                wall.remove()
                log(f"pane {name} ended")
        for name in names:
            if name not in self.walls:
                self.make(name)
        font = self.real_font()
        refont = font != getattr(self, "told_font", None)
        self.told_font = font
        for name, r in rects.items():
            if refont or self.rects.get(name, (None,) * 4)[2:] != r[2:]:
                self.walls[name].set_box(self.box(r[2], r[3]))
        moved = [n for n, r in rects.items() if self.rects.get(n) != r]
        self.rects = rects
        for name in moved:
            self.replace_pictures(name)
        self.area = (cols, rows)

    # -- commands, said to a pane by a controller

    def commands_for(self, name):
        return {"split": lambda m: self.cmd_split(name, m),
                "unsplit": lambda m: self.cmd_unsplit(name, m),
                "share": lambda m: self.cmd_share(name, m),
                "layout": lambda m: self.cmd_layout()}

    def find(self, name):
        return next((lf for lf in leaves(self.tree) if lf.name == name), None)

    def cmd_split(self, name, m):
        if len(m.args) < 2:
            raise ValueError("needs a direction and a name")
        way, new = m.args[0], m.args[1]
        if way not in ("right", "down", "left", "up"):
            raise ValueError(f"{protocol_word(way)}: not right, down, left or up")
        if not NAME.match(new) or len(self.wall_of(new)) > 32:
            raise ValueError(f"{protocol_word(new)}: not a pane name")
        share = 50
        if len(m.args) > 2:
            if not m.args[2].isdigit() or not 5 <= int(m.args[2]) <= 95:
                raise ValueError(f"{protocol_word(m.args[2])}: not a share from 5 to 95")
            share = int(m.args[2])
        with self.lock:
            if self.find(new):
                raise ValueError(f"{new}: name in use")
            old = self.find(name)
            fresh = Leaf(new)
            h = "h" if way in ("right", "left") else "v"
            cut = Cut(h, 100 - share, old, fresh) if way in ("right", "down") \
                else Cut(h, share, fresh, old)
            up = parent_of(self.tree, old)
            if up is None:
                tree = cut
            else:
                tree = self.tree
                if up.a is old:
                    up.a = cut
                else:
                    up.b = cut
            if self.area:
                rects = lay(tree, 0, 0, *self.area, {})
                if any(w < LEAST[0] or h < LEAST[1] for _, _, w, h in rects.values()):
                    # Undone: the tree is only changed when it fits.
                    if up is not None:
                        if up.a is cut:
                            up.a = old
                        else:
                            up.b = old
                    raise ValueError(f"{new}: too small, under {LEAST[0]}x{LEAST[1]}")
            self.tree = tree
            self.changed_tree = True
        log(f"split {name} {way} {new} {share}%")
        return [f"split {new} {way}-of={name} share={share}"]

    def cmd_unsplit(self, name, m):
        with self.lock:
            me = self.find(name)
            up = parent_of(self.tree, me)
            if up is None:
                raise ValueError(f"{name}: the last pane")
            other = up.b if up.a is me else up.a
            grand = parent_of(self.tree, up)
            if grand is None:
                self.tree = other
            elif grand.a is up:
                grand.a = other
            else:
                grand.b = other
            self.changed_tree = True
        log(f"unsplit {name}")
        return [f"unsplit {name}"]

    def cmd_share(self, name, m):
        if not m.args or not m.args[0].isdigit() or not 5 <= int(m.args[0]) <= 95:
            raise ValueError("needs a share from 5 to 95")
        pct = int(m.args[0])
        with self.lock:
            me = self.find(name)
            up = parent_of(self.tree, me)
            if up is None:
                raise ValueError(f"{name}: the whole wall")
            up.share = pct if up.a is me else 100 - pct
            self.changed_tree = True
        return [f"share {name} {pct}"]

    def cmd_layout(self):
        with self.lock:
            rects = dict(self.rects)
        out = [f"pane {n} {w}x{h} at={x},{y} wall={self.wall_of(n)}"
               for n, (x, y, w, h) in rects.items()]
        return out + [f"panes {len(out)}"]

    # -- composing

    def render(self, cols, rows):
        if (cols, rows) != self.area or self.changed_tree \
                or self.real_font() != getattr(self, "told_font", None):
            self.changed_tree = False
            self.relayout(cols, rows)
            self.save()
        if self.resend:
            self.resend = False
            for name in list(self.walls):
                self.replace_pictures(name, again=True)
        out = [[bitwall.cell(" ")] * cols for _ in range(rows)]
        for name, (x, y, w, h) in self.rects.items():
            wall = self.walls.get(name)
            if wall is None:
                continue
            if not wall.connected:
                self.empty(out, name, x, y, w, h)
                continue
            with wall._lock:
                g = wall.grid
                rows_ = [(g.ch[r][:w], g.fgs[r][:w], g.bgs[r][:w], g.ats[r][:w])
                         for r in range(min(h, g.rows))]
            for r, (chs, fgs, bgs, ats) in enumerate(rows_):
                line = out[y + r]
                for c in range(min(w, len(chs))):
                    a = ats[c]
                    line[x + c] = bitwall.cell(
                        chs[c] or " ", colour(fgs[c]), colour(bgs[c]),
                        bold=bool(a & gridmod.BOLD), italics=bool(a & gridmod.ITALIC),
                        underscore=bool(a & gridmod.UNDER),
                        strikethrough=bool(a & gridmod.STRIKE),
                        reverse=bool(a & gridmod.REVERSE), faint=bool(a & gridmod.FAINT))
        return out

    def empty(self, out, name, x, y, w, h):
        """A pane nobody is sending to says its wall's name."""
        text = f"{self.host}:{self.wall_of(name)}"[:w]
        row = y + h // 2
        for i, ch in enumerate(text):
            out[row][x + (w - len(text)) // 2 + i] = bitwall.cell(ch, DIM, "default")

    # -- a pane's picture of its glass

    def look(self, name, w, h, done):
        """A pane's glass at w by h, as QOI to `done`: the real wall's
        picture of its glass, the pane's rectangle cut out of it. False,
        not offered, where the real wall takes no pictures or there is no
        Pillow to cut it with."""
        try:
            import PIL                                          # noqa: F401
        except ImportError:
            return False
        if self.w is None or not self.w.offers("picture"):
            return False
        threading.Thread(target=self._look, args=(name, w, h, done), daemon=True).start()
        return True

    def _look(self, name, w, h, done):
        import io
        from PIL import Image
        data = None
        try:
            with self.ctl_lock:
                if self.ctl is None:
                    kw = {"run": self.run} if self.run else {}
                    self.ctl = bitwall.Controller(self.target, **kw).open()
                got = self.ctl.ask("picture glass")
            blob = next((b for b in got if hasattr(b, "data")), None)
            rect, sc = self.rects.get(name), self.screen()
            if blob is not None and rect is not None and self.area:
                img = Image.open(io.BytesIO(blob.data)).convert("RGBA")
                said = self.w._screen()
                px = (said.get("pixels") if said is not None else "") or ""
                gw, _, gh = px.partition("x")
                gw, gh = (int(gw), int(gh)) if gw.isdigit() and gh.isdigit() else img.size
                cw, ch = sc["cell"]
                cols, rows = self.area
                # The grid is centred on the glass, as the painter and the
                # page both lay it; a picture of another size is scaled to.
                ox, oy = max(0, (gw - cols * cw) // 2), max(0, (gh - rows * ch) // 2)
                fx, fy = img.size[0] / gw, img.size[1] / gh
                x, y, pw, ph = rect
                box = (round((ox + x * cw) * fx), round((oy + y * ch) * fy),
                       round((ox + (x + pw) * cw) * fx), round((oy + (y + ph) * ch) * fy))
                out = io.BytesIO()
                img.crop(box).resize((w, h)).save(out, "QOI")
                data = out.getvalue()
        except Exception as e:                                  # noqa: BLE001
            log(f"picture of {name}: {type(e).__name__}: {e}")
            self.ctl = None
        done(data)

    # -- kitty pictures

    def slot(self, name):
        if name not in self.slots:
            self.slots[name] = len(self.slots) + 1
        return self.slots[name]

    def ident(self, name, key):
        return KITTY_BASE + (self.slot(name) << 16) + (key & 0xFFFF)

    def kitty(self, name, head, data):
        """What a pane's core read of its sender's pictures, sent on."""
        t = head.get("t")
        if t == "kimg":
            # Kept, and sent whole with each placement: herdr draws a
            # picture sent with `a=T` and drops one uploaded with `a=t`
            # and placed after with `a=p`.
            self.images.setdefault(name, {})[head["key"]] = (head["f"], head["w"], head["h"], data)
        elif t == "kgone":
            self.images.get(name, {}).pop(head["key"], None)
            self.send(f"\x1b_Ga=d,d=I,i={self.ident(name, head['key'])},q=2\x1b\\")
            self.placed.get(name, {}).pop(head["key"], None)
        elif t == "kplaces":
            self.places[name] = head["list"]
            self.replace_pictures(name)

    def unplace(self, name):
        gone = self.placed.pop(name, {})
        if gone:
            self.send("".join(f"\x1b_Ga=d,d=I,i={self.ident(name, k)},q=2\x1b\\"
                              for k in sorted(gone)))

    def replace_pictures(self, name, again=False):
        """A pane's pictures on the real wall brought to what its sender has
        placed: each one new or moved sent, at the pane's offset and cropped
        to it, and only then each one gone deleted -- so on a slow link the
        picture being replaced stays up until its successor has arrived,
        and one that has not changed is never sent again. `again` sends
        every one, to a real wall that has come back holding none."""
        had = {} if again else self.placed.get(name, {})
        rect = self.rects.get(name)
        want = {}
        images = self.images.get(name, {})
        if rect is not None:
            px, py, pw, ph = rect
            for key, col, row, c, r, z, iw, ih in self.places.get(name, []):
                vc, vr = min(c, pw - col), min(r, ph - row)
                if vc <= 0 or vr <= 0 or col < 0 or row < 0 or key not in images:
                    continue
                keys = f"c={vc},r={vr},z={z},p=1,C=1"
                if (vc, vr) != (c, r):
                    keys += f",x=0,y=0,w={max(1, iw * vc // c)},h={max(1, ih * vr // r)}"
                want[key] = ((px + col, py + row, keys), keys)
        out = []
        for key, (sig, keys) in want.items():
            if had.get(key) == sig:
                continue
            i = self.ident(name, key)
            if key in had:
                # The same id somewhere else: taken down before it is put
                # up again, as kitty keeps one image to an id.
                out.append(f"\x1b_Ga=d,d=I,i={i},q=2\x1b\\")
            f, w, h, data = images[key]
            out.append(f"\x1b[{sig[1] + 1};{sig[0] + 1}H" + transmit(i, f, w, h, data, keys))
        out += [f"\x1b_Ga=d,d=I,i={self.ident(name, k)},q=2\x1b\\"
                for k in sorted(had) if k not in want]
        self.placed[name] = {k: sig for k, (sig, _) in want.items()}
        if out:
            self.send("".join(out))

    def send(self, text):
        if self.w is not None and self.w.offers("kitty"):
            self.w.passthrough(text.encode())

    def take_down_pictures(self):
        """Every picture a pane put on the real wall, deleted on the way
        out: a terminal keeps a picture after the sender that sent it."""
        ids = {self.ident(n, k) for n, imgs in self.images.items() for k in imgs}
        if ids and self.w is not None and self.w.offers("kitty"):
            self.w.passthrough("".join(f"\x1b_Ga=d,d=I,i={i},q=2\x1b\\"
                                       for i in sorted(ids)).encode())

    # -- each second

    def tick(self):
        """Watching, taps and meta, carried between the real wall and the
        panes."""
        w = self.w
        watched = 0 if w.watching() == 0 else 1
        for wall in list(self.walls.values()):
            wall.set_visible(watched)
        while (press := w.input()) is not None:
            if press.get("kind") != "tap":
                continue
            x, y = press["cell"]
            for name, (px, py, pw, ph) in self.rects.items():
                if px <= x < px + pw and py <= y < py + ph:
                    self.walls[name].press("tap", [("cell", f"{x - px},{y - py}"),
                                                   ("area", f"{pw},{ph}")])
                    break
        names = []
        for name, wall in self.walls.items():
            b = wall.base
            said = b.meta.get("name") if b is not None and b.open else None
            names.append(said[0] if said else name)     # (value, when, hidden)
        title = " + ".join(names)
        if title != getattr(self, "_title", None):
            self._title = title
            w.meta(name=title, description=f"{len(names)} panes: " + ", ".join(
                f"{self.host}:{self.wall_of(n)}" for n in self.walls))

    def stop(self):
        if self.ctl is not None:
            try:
                self.ctl.close()
            except Exception:                                   # noqa: BLE001
                pass
        for name, wall in list(self.walls.items()):
            wall.remove()
        self.walls.clear()


def colour(v):
    return "default" if v == gridmod.DEFAULT else f"{v:06x}"


def protocol_word(text):
    return protocol.word(text)


def transmit(ident, f, w, h, data, place):
    """A picture sent whole to the real wall and placed at the cursor,
    `place` its placement's keys."""
    b64 = base64.b64encode(data).decode()
    parts = [b64[i:i + 4096] for i in range(0, len(b64), 4096)] or [""]
    keys = f"a=T,f={f},i={ident},{place},q=2" + ("" if f == 100 else f",s={w},v={h}")
    out = []
    for k, part in enumerate(parts):
        more = 1 if k < len(parts) - 1 else 0
        head = f"{keys},m={more}" if k == 0 else f"m={more}"
        out.append(f"\x1b_G{head};{part}\x1b\\")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="main",
                    help="the first pane's name, where no layout is kept (default %(default)s)")
    ap.add_argument("--font", type=int, default=FONT,
                    help="the real wall's font, every pane's (default %(default)s)")
    ap.add_argument("--run", help="the real wall's own directory, if not where its host keeps it")
    ap.add_argument("target", help="the real wall, host:wall")
    a = ap.parse_args()
    if not NAME.match(a.name):
        sys.exit(f"panes: {a.name!r} is not a pane name")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    p = Panes(a.target, a.name, a.font, run=a.run)
    try:
        w = p.attach()
    except Exception as e:                                      # noqa: BLE001
        log(f"could not attach to {a.target}: {e}")
        return 1
    log(f"attached to {a.target}, font {a.font}, {w.area()[0]}x{w.area()[1]}; "
        f"layout in {p.keep}")
    while not stop.wait(0.5):
        if w.stopped:
            log(f"left {a.target}: {w.stopped}")
            break
        try:
            p.tick()
        except Exception as e:                                  # noqa: BLE001
            log(f"tick: {type(e).__name__}: {e}")
    log("stopping")
    w.detach()
    p.stop()
    log("detached; panes taken down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
