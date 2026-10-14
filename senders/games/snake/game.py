"""Self-playing snake: state, AI and drawing, in plain Python.

The core owns the game and hands back a grid of `Glyph` -- a character with
256-palette colours. It knows nothing about walls and nothing about
terminals -- of bitwall it takes only the letters, for the score -- so the
same game drives a wall through `main.py` and a tmux pane through
`pane-snake`; each is a few lines that turn this grid into cells or into
ANSI.

**Two cells per square.** A terminal cell is about twice as tall as it is
wide, so a snake one cell across reads as a thread. Every square of the
board is a pair of cells, which makes the body chunky enough to follow from
across a room and keeps the field roughly square.

**The player.** Breadth-first to the food, taken only when a simulation of
walking that whole path leaves the snake still able to reach its own tail;
otherwise it keeps to the largest room it can find and stays near its tail,
which is the corridor it is about to vacate. Two things stop it being a
solved puzzle to watch: a small chance of a wrong turn at any junction, and
a hunger clock that drops the safety check once it has gone too long without
eating, so a snake circling its own tail forever is not a state it can
reach. It dies, and dying is part of the show.
"""
import os
import random
import sys
import time
from collections import deque, namedtuple

_HERE = os.path.dirname(os.path.abspath(__file__))
# bitwall's letters are the repo's one set of big glyphs, and the score is
# meant to be read across a room.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(_HERE))))

from bitwall import letters                                     # noqa: E402

Glyph = namedtuple("Glyph", "ch fg bg bold")
BLANK = Glyph(" ", None, None, False)


def block(text):
    """`text` in 3x5 block letters, a cell a pixel: rows in which a cell
    is ink or None. Mono, so a score that changes keeps its shape."""
    return letters.rows(text, letters.BLOCK, spacing="mono", cell=1.0)


def block_width(text):
    return letters.size(text, letters.BLOCK, spacing="mono", cell=1.0)[0]

# A hue wheel picked out of the 256-palette in order, so stepping through it
# reads as one colour travelling rather than a set of unrelated colours.
HUE = (196, 202, 208, 214, 220, 226, 190, 154, 118, 82,
       46, 47, 48, 49, 50, 51, 45, 39, 33, 27,
       21, 57, 93, 129, 165, 201, 200, 199, 198, 197)

FIELD_BG = 233            # the board reads as a panel, not as the terminal
HEAD = 231
LABEL = 244
DIM = 240
DEAD = 196
BANNER_BG = 16

FIELD = Glyph(" ", None, FIELD_BG, False)

# The food is a gem, beating through a speck, a hollow glint and a solid
# stone. A square of the board is a pair of cells, and two diamonds meet at
# their points, so the food holds together as one shape across the whole
# square; and being angular where the snake is rectangular, it is the one
# thing on the field that cannot be mistaken for a piece of the body.
FOOD_CHARS = ("·", "◇", "◆", "◆", "◆", "◆", "◇", "·")
FOOD_HUE = (226, 226, 220, 214, 208, 202, 208, 220)
# A spark is a bright speck thrown off the gem and shrinking: round and
# solid, so a burst scatters rather than reading as more gems.
SPARK_CHARS = ("●", "•", "▪", "·")
FADE = ("█", "▓", "▒", "░")

TRAIL_SECS = 0.45         # how long a vacated square keeps glowing
FLASH_SECS = 0.7
DISSOLVE_SECS = 1.3
DEATH_SECS = 3.4


class Canvas:
    """A fixed grid that clips instead of raising.

    The area comes from whatever is showing the game and can change under a
    frame, so everything placed here is clipped: a layout slightly too big
    for its area degrades rather than killing the stream.
    """

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.g = [[BLANK] * cols for _ in range(rows)]

    def put(self, x, y, ch, fg=None, bg=None, bold=False):
        if 0 <= x < self.cols and 0 <= y < self.rows:
            self.g[y][x] = Glyph(ch, fg, bg, bold)

    def text(self, x, y, s, fg=None, bg=None, bold=False):
        for i, ch in enumerate(s):
            self.put(x + i, y, ch, fg, bg, bold)
        return x + len(s)

    def fill(self, x, y, w, h, glyph):
        for yy in range(max(0, y), min(self.rows, y + h)):
            row = self.g[yy]
            for xx in range(max(0, x), min(self.cols, x + w)):
                row[xx] = glyph


class Game:
    """One long-running game of snake, restarting itself forever."""

    MISTAKE = 0.015           # chance of a wrong turn at a junction
    STEP_SLOW, STEP_FAST = 0.085, 0.045

    def __init__(self):
        self.cols = self.rows = 0
        self.gw = self.gh = 0
        self.high = 0
        self.games = 0
        self.snake = deque()
        self.food = None
        self._new_game(time.monotonic())

    # ------------------------------------------------------------ geometry

    def _fit(self, cols, rows):
        """Lay the board out for an area, and start over if it changed.

        A resize is a different board, not the same game on a new one: the
        snake would be somewhere off the edge of it. The high score and the
        game count survive, because they belong to the session rather than
        to the board.
        """
        if (cols, rows) == (self.cols, self.rows):
            return
        self.cols, self.rows = cols, rows
        # A tall header carries the score in block glyphs; a short area gets
        # one line of ordinary text instead.
        self.hud = 6 if rows >= 16 and cols >= 44 else 1
        # The border is the edge of the pane, not the edge of the board:
        # a square is two cells wide, so the board is a cell short whenever
        # the space for it is odd, and a border drawn around the board
        # would sit inside the pane by that cell.
        self.bx0, self.bx1 = 0, cols - 1
        self.by0, self.by1 = self.hud, rows - 1
        inner_w, inner_h = cols - 2, rows - self.hud - 2
        self.gw = max(0, inner_w // 2)
        self.gh = max(0, inner_h)
        self.ox = 1 + max(0, (inner_w - self.gw * 2) // 2)
        self.oy = self.hud + 1
        self.games += 1
        self._new_game(time.monotonic())

    def playable(self):
        return self.gw >= 8 and self.gh >= 6

    # --------------------------------------------------------------- state

    def _new_game(self, now):
        self.score = 0
        self.eaten = 0
        self.hungry = 0
        self.alive = True
        self.won = False
        self.new_high = False
        self.dead_at = 0.0
        self.last = now
        self.trail = {}
        self.sparks = []
        if not self.playable():
            self.snake = deque()
            self.food = None
            return
        x, y = self.gw // 4, self.gh // 2
        self.snake = deque([(x, y), (x - 1, y), (x - 2, y)])
        self._place_food()

    def _place_food(self):
        body = set(self.snake)
        free = [(x, y) for y in range(self.gh) for x in range(self.gw)
                if (x, y) not in body]
        if not free:
            self.food = None
            self.won = True
            return False
        self.food = random.choice(free)
        return True

    @property
    def interval(self):
        """Faster as it grows, so a long game does not drag.

        Full speed at 120 segments, or at a quarter of a board too small to
        hold that many, so a pane accelerates on the same curve a wall does.
        """
        full = max(15, min(120, self.gw * self.gh * 0.25))
        t = min(1.0, len(self.snake) / full)
        return self.STEP_SLOW + (self.STEP_FAST - self.STEP_SLOW) * t

    # ------------------------------------------------------------ the board

    def _nbrs(self, p):
        x, y = p
        if x + 1 < self.gw:
            yield (x + 1, y)
        if x:
            yield (x - 1, y)
        if y + 1 < self.gh:
            yield (x, y + 1)
        if y:
            yield (x, y - 1)

    def _path(self, start, goal, blocked):
        """Shortest route from start to goal, or None."""
        if start == goal:
            return [start]
        prev = {start: None}
        q = deque((start,))
        while q:
            cur = q.popleft()
            for n in self._nbrs(cur):
                if n in prev or n in blocked:
                    continue
                prev[n] = cur
                if n == goal:
                    path = [n]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return path[::-1]
                q.append(n)
        return None

    def _room(self, start, blocked, cap):
        """Squares reachable from start, counted no further than cap.

        The question is only ever "is there room for a snake this long", so
        counting past that is work with nothing riding on it.
        """
        seen = {start}
        q = deque((start,))
        n = 0
        while q:
            n += 1
            if n >= cap:
                return n
            for p in self._nbrs(q.popleft()):
                if p not in seen and p not in blocked:
                    seen.add(p)
                    q.append(p)
        return n

    def _walk(self, path):
        """The body left after walking `path`, which ends on the food."""
        body = deque(self.snake)
        for p in path[1:]:
            body.appendleft(p)
            if p != self.food:
                body.pop()
        return body

    def _reaches_tail(self, body):
        """Can that body still get back to its own tail?

        The tail is the one square guaranteed to open up, so a head that can
        reach it has somewhere to go for as many moves as the body is long.
        A snake sealed away from its own tail is in a pocket it will fill.
        """
        if len(body) < 3:
            return True
        tail = body[-1]
        return self._path(body[0], tail, set(body) - {tail}) is not None

    def _choose(self):
        """The next square, or None when the snake is walled in."""
        head, tail = self.snake[0], self.snake[-1]
        body = set(self.snake)
        neck = self.snake[1] if len(self.snake) > 1 else None
        # The tail vacates as the head arrives, so it counts as free; the
        # neck never does.
        cands = [n for n in self._nbrs(head)
                 if n != neck and (n not in body or n == tail)]
        if not cands:
            return None

        starving = self.hungry > 4 * (self.gw + self.gh)
        path = self._path(head, self.food, body - {tail})
        if path and len(path) > 1 and path[1] in cands:
            if starving or self._reaches_tail(self._walk(path)):
                return self._perhaps(path[1], cands)

        # No route it trusts: hold the most open square, and of those the
        # one nearest the tail, which is the corridor about to open up.
        free = body - {tail}
        cap = len(self.snake) + 2
        best = max(cands, key=lambda n: (self._room(n, free, cap),
                                         -self._dist(n, tail)))
        return self._perhaps(best, cands)

    def _perhaps(self, best, cands):
        """The best move, or now and then one of the others.

        A player that never errs has nothing to watch.
        """
        others = [c for c in cands if c != best]
        if others and random.random() < self.MISTAKE:
            return random.choice(others)
        return best

    @staticmethod
    def _dist(a, b):
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    # --------------------------------------------------------------- clock

    def _step(self, now):
        nxt = self._choose()
        if nxt is None:
            self._die(now)
            return
        self.hungry += 1
        n = len(self.snake)
        hue = HUE[int(now * 10) % len(HUE)]
        self.snake.appendleft(nxt)
        if nxt == self.food:
            self.eaten += 1
            self.score += 10 + n // 4
            self.hungry = 0
            self._spark(nxt, now)
            if not self._place_food():
                self._die(now)
        else:
            self.trail[self.snake.pop()] = (now, hue)
        self.trail = {k: v for k, v in self.trail.items()
                      if now - v[0] < TRAIL_SECS}
        self.sparks = [s for s in self.sparks if s[3] > now]

    def _spark(self, at, now):
        x, y = at
        for _ in range(12):
            self.sparks.append((x + random.randint(-2, 2),
                                y + random.randint(-1, 1),
                                now, now + random.uniform(0.25, 0.6)))

    def _die(self, now):
        self.alive = False
        self.dead_at = now
        if self.score > self.high:
            self.high = self.score
            self.new_high = self.score > 0

    def _advance(self, now):
        if not self.playable():
            return
        if not self.alive:
            if now - self.dead_at >= DEATH_SECS:
                self.games += 1
                self._new_game(now)
            return
        # A stall -- a laptop lid, a slow frame -- must not buy a burst of
        # moves that teleports the snake across the board.
        if now - self.last > 1.0:
            self.last = now
        step = self.interval
        while self.alive and now - self.last >= step:
            self.last += step
            self._step(now)

    # -------------------------------------------------------------- drawing

    def frame(self, cols, rows):
        """An area in, a grid of Glyph out. The whole of the public surface."""
        self._fit(cols, rows)
        now = time.monotonic()
        self._advance(now)
        c = Canvas(cols, rows)
        if not self.playable():
            c.text(1, max(0, rows // 2), "too small"[:max(0, cols - 1)],
                   fg=DEAD, bold=True)
            return c.g
        self._draw_hud(c, now)
        self._draw_border(c, now)
        self._draw_field(c, now)
        if not self.alive and now - self.dead_at >= 1.0:
            self._draw_banner(c, now)
        return c.g

    def _hue_at(self, i, phase):
        return HUE[int(i + phase) % len(HUE)]

    def _draw_border(self, c, now):
        """A box whose colour chases round it, so the board is never still."""
        x0, y0 = self.bx0, self.by0
        x1, y1 = self.bx1, self.by1
        phase = int(now * 5)
        dying = not self.alive and (now - self.dead_at) < FLASH_SECS

        def col(k):
            if dying:
                return DEAD if int(now * 12) % 2 else HEAD
            return self._hue_at(k // 4, phase)

        k = 0
        for x in range(x0, x1 + 1):
            c.put(x, y0, "━", fg=col(k))
            k += 1
        for y in range(y0 + 1, y1 + 1):
            c.put(x1, y, "┃", fg=col(k))
            k += 1
        for x in range(x1 - 1, x0 - 1, -1):
            c.put(x, y1, "━", fg=col(k))
            k += 1
        for y in range(y1 - 1, y0, -1):
            c.put(x0, y, "┃", fg=col(k))
            k += 1
        c.put(x0, y0, "┏", fg=col(0))
        c.put(x1, y0, "┓", fg=col(x1 - x0))
        c.put(x1, y1, "┛", fg=col(x1 - x0 + y1 - y0))
        c.put(x0, y1, "┗", fg=col(2 * (x1 - x0) + y1 - y0))

    def _draw_field(self, c, now):
        ox, oy = self.ox, self.oy
        for gy in range(self.gh):
            row = c.g[oy + gy]
            for x in range(ox, ox + self.gw * 2):
                row[x] = FIELD

        def pair(gx, gy, ch, fg, bold=False):
            c.put(ox + gx * 2, oy + gy, ch, fg, FIELD_BG, bold)
            c.put(ox + gx * 2 + 1, oy + gy, ch, fg, FIELD_BG, bold)

        # Squares the snake has just left keep glowing in the colour they
        # were, which is what makes the movement read as motion.
        for (gx, gy), (t0, hue) in self.trail.items():
            age = (now - t0) / TRAIL_SECS
            if 0 <= age < 1 and 0 <= gx < self.gw and 0 <= gy < self.gh:
                pair(gx, gy, FADE[2 + int(age * 2)], hue)

        for (gx, gy, t0, t1) in self.sparks:
            if 0 <= gx < self.gw and 0 <= gy < self.gh:
                age = (now - t0) / max(1e-6, t1 - t0)
                pair(gx, gy, SPARK_CHARS[max(0, min(3, int(age * 4)))],
                     FOOD_HUE[int(now * 8) % 8], bold=True)

        if self.food and self.alive:
            i = int(now * 8) % 8
            pair(self.food[0], self.food[1], FOOD_CHARS[i], FOOD_HUE[i],
                 bold=True)

        self._draw_snake(now, pair)

    def _draw_snake(self, now, pair):
        n = len(self.snake)
        if not n:
            return
        phase = now * 10
        span = len(HUE) / n           # the rainbow spans the body exactly once
        d = now - self.dead_at
        flashing = not self.alive and d < FLASH_SECS
        gone = 0.0
        if not self.alive and d >= FLASH_SECS:
            gone = min(1.0, (d - FLASH_SECS) / DISSOLVE_SECS) * n

        for i, (gx, gy) in enumerate(self.snake):
            from_tail = n - 1 - i
            ch = "█"
            if gone:
                # Thinning out from the tail forward, so the head is the
                # last of it to go.
                if from_tail < gone - 3:
                    continue
                if from_tail < gone:
                    ch = FADE[min(3, int(gone - from_tail))]
            elif from_tail == 0:
                ch = "▒"                      # the tail tapers to a point
            elif from_tail == 1:
                ch = "▓"
            if flashing:
                fg = DEAD if int(now * 12) % 2 else HEAD
            elif i == 0:
                fg = HEAD                     # the head leads in white
            else:
                fg = self._hue_at(i * span, phase)
            pair(gx, gy, ch, fg, bold=(i == 0))

    # ----------------------------------------------------------------- hud

    def _draw_hud(self, c, now):
        phase = int(now * 5)
        if self.hud == 1:
            x = c.text(1, 0, "SCORE ", fg=LABEL)
            x = c.text(x, 0, str(self.score), fg=HEAD, bold=True)
            x = c.text(x + 2, 0, "HIGH ", fg=LABEL)
            x = c.text(x, 0, str(self.high), fg=226)
            x = c.text(x + 2, 0, "LEN ", fg=LABEL)
            x = c.text(x, 0, str(len(self.snake)), fg=DIM)
            c.text(x + 2, 0, f"GAME {self.games}", fg=DIM)
            return

        def big(text, x, y, bright):
            for dy, line in enumerate(block(text)):
                for dx, ink in enumerate(line):
                    if ink is not None:
                        hue = self._hue_at(dx // 2, phase) if bright else DIM
                        c.put(x + dx, y + dy, "█", fg=hue, bold=True)

        score, high = str(self.score), str(self.high)
        c.text(2, 0, "SCORE", fg=LABEL)
        big(score, 2, 1, True)

        # Right-aligned, but never back over the score: on a narrow area a
        # clipped high score beats two numbers drawn through each other.
        hw = block_width(high)
        hx = max(4 + block_width(score), self.cols - 2 - hw)
        c.text(max(2, self.cols - 2 - len("HIGH")), 0, "HIGH", fg=LABEL)
        big(high, hx, 1, False)

        mid = f"LEN {len(self.snake)}   GAME {self.games}   ATE {self.eaten}"
        c.text(max(2, (self.cols - len(mid)) // 2), 0, mid, fg=DIM)

        title = "SNAKE"
        tw = block_width(title)
        left = 2 + block_width(score)
        right = hx
        if right - left > tw + 8:
            big(title, left + (right - left - tw) // 2, 1, True)

    def _draw_banner(self, c, now):
        """What is left on the screen once the snake has gone."""
        words = ("YOU WIN", "WIN") if self.won else ("GAME OVER", "OVER")
        field_w = self.gw * 2
        text = next((w for w in words if block_width(w) + 6 <= field_w),
                    None)
        sub = (f"NEW HIGH  {self.high}" if self.new_high
               else f"SCORE  {self.score}")
        if text is None or self.gh < 11:
            x = self.ox + max(0, (field_w - len(sub) - 11) // 2)
            y = self.oy + self.gh // 2
            c.text(x, y, "GAME OVER  " + sub, fg=DEAD, bg=BANNER_BG, bold=True)
            return

        bw = block_width(text)
        w, h = bw + 6, 11
        x = self.ox + (field_w - w) // 2
        y = self.oy + (self.gh - h) // 2
        c.fill(x, y, w, h, Glyph(" ", None, BANNER_BG, False))
        edge = DEAD if self.new_high and int(now * 6) % 2 else self._hue_at(
            0, int(now * 5))
        for i in range(w):
            c.put(x + i, y, "━", fg=edge, bg=BANNER_BG)
            c.put(x + i, y + h - 1, "━", fg=edge, bg=BANNER_BG)
        for i in range(1, h - 1):
            c.put(x, y + i, "┃", fg=edge, bg=BANNER_BG)
            c.put(x + w - 1, y + i, "┃", fg=edge, bg=BANNER_BG)
        c.put(x, y, "┏", fg=edge, bg=BANNER_BG)
        c.put(x + w - 1, y, "┓", fg=edge, bg=BANNER_BG)
        c.put(x, y + h - 1, "┗", fg=edge, bg=BANNER_BG)
        c.put(x + w - 1, y + h - 1, "┛", fg=edge, bg=BANNER_BG)

        phase = int(now * 8)
        for dy, line in enumerate(block(text)):
            for dx, ink in enumerate(line):
                if ink is not None:
                    c.put(x + 3 + dx, y + 2 + dy, "█",
                          fg=self._hue_at(dx // 2, phase), bg=BANNER_BG,
                          bold=True)
        c.text(x + (w - len(sub)) // 2, y + 8, sub,
               fg=226 if self.new_high else LABEL, bg=BANNER_BG, bold=True)
