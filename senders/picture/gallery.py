"""Pictures to hang, fetched and kept on disk: works from the Art Institute
of Chicago, and photographs from NASA and paintings from Pepper&Carrot,
and whatever further series the host keeps in `~/.bitwall/picture.json`.

The collection half of the picture sender. It answers one question -- give
me another work from this series -- and knows nothing about walls, cells or
bitwall, so the same gallery could feed a picture frame or a web page.

**Why this museum.** Its open-access set is about fifty thousand works, the
API needs no key, and every image is served over IIIF, which means the size
is part of the URL: a wall wants a picture a few hundred pixels wide and
asking for that is one request rather than a six-thousand-pixel download
thrown away in a scaler.

**Why these series.** A character grid is a few hundred pixels across and
every pixel is two colours shared with its neighbours, so what survives is
flat colour, hard outlines and strong shapes, and what dissolves is
continuous tone and fine line. That is not a compromise here: the museum's
public-domain mass is Japanese woodblock print, which is exactly the first
kind. Hiroshige alone is sixteen hundred works, all of them flat inked
areas with a drawn edge. The series below are ordered by how well they take
the reduction, and the painting series are included knowing they take it
least well, because they are what people want to see.

**Searching, not listing.** A series is an Elasticsearch query rather than a
list of ids, so it stays right as the museum's open-access set grows, and a
work that loses its public-domain flag stops appearing rather than 404ing.

**Beyond the museum.** A series need not be the museum's: each has a
source, and a source is anything that can pick a work and say where its
image is. NASA's image library is public domain and needs no key, and
its searches are chosen for pictures that are a shape at a glance -- a
nebula, a planet, the curve of the Earth -- rather than the people and
hardware that make up most of it. David Revoy's Pepper&Carrot is CC-BY,
so its caption names him; it is painted in flat colour too, which is why
it is here.

**Series of one's own.** The series here are the ones anybody may show.
A host adds its own in `~/.bitwall/picture.json`, or the file
`BITWALL_PICTURE` names, as data rather than code: a numbered set of
stills, which is a URL with a set's name and a number in it, the sets
and what each is called, and how many each has (`Numbered`). The file's
`categories` add a category or replace one of the same name:

    {"series": {"stills": {"source": "numbered", "name": "The Studio",
                           "url": "https://example.org/{set}{n:03d}.jpg",
                           "count": 50, "sets": {"one": ["Film One", "1990"]},
                           "why": "what it is and why it suits a grid"}},
     "categories": {"films": {"series": ["stills"], "what": "film stills"}}}

**Categories.** What a person picks is a category: a handful of series
that belong together, rotated through. `all` is the museum's art, the
rotation the sender has always shown when not told otherwise.

**One image, kept.** Downloads land in `media/pictures` under the image's
own id and are never fetched twice. The museum asks that a client identify
itself, which `AGENT` does; it carries no address, because an address is a
personal detail and the museum has not asked for one.
"""
import html
import json
import os
import random
import re
import struct
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(os.path.dirname(os.path.dirname(_HERE)),
                     "media", "pictures")

API = "https://api.artic.edu/api/v1/artworks/search"
IIIF = "https://www.artic.edu/iiif/2/%s/full/%d,/0/default.jpg"
AGENT = "bitwall-picture/1.0 (a character-grid picture frame)"

#: How wide an image is asked for. A wall's framebuffer is at most a few
#: hundred pixels across, so this is already several times what any mode
#: can use; it is the museum's own default size, which makes it the one
#: most likely to be sitting in their cache rather than generated.
WIDTH = 843

#: How many works a series draws from. The query is sorted by relevance and
#: the deeper pages of a broad query are the weaker matches, so a series
#: takes a random page from within this many and a random work from it: the
#: same series gives a different picture each run without ever reaching the
#: part of the result set that stopped being about the series.
DEPTH = 400
PAGE = 100


def _q(*must, **kw):
    """A query for public-domain works that actually have an image."""
    return {"bool": {
        "must": [{"term": {"is_public_domain": True}}, *must],
        "filter": [{"exists": {"field": "image_id"}}, *kw.get("filter", [])]}}


def _fold(name):
    """The name as the museum's keyword index holds it: lowercase, plain.

    `artist_title.keyword` is the whole name as one token, which is what
    makes it exact -- a phrase match on the analysed field also matches
    "Imitator of Vincent van Gogh" and "Follower of", and a series called
    vangogh that quietly serves imitations is worse than one that is
    empty. The index folds accents away, so the query has to as well.
    """
    return (unicodedata.normalize("NFKD", name)
            .encode("ascii", "ignore").decode().lower())


def _by(artist, *must):
    return _q({"term": {"artist_title.keyword": _fold(artist)}}, *must)


def _kind(word):
    return _q({"match_phrase": {"classification_title": word}})


def _anyone(names, *must):
    """Works by any of several artists, as one series."""
    return _q({"terms": {"artist_title.keyword": [_fold(n) for n in names]}},
              *must)


PAINTING = {"match_phrase": {"artwork_type_title": "Painting"}}

#: The museum's own mark for a work it considers notable. It is what
#: drives their front page and their audio tours, so it is a curator's
#: answer to "which of these would somebody recognise" rather than ours,
#: and it is the only such signal the API offers.
NOTABLE = {"term": {"is_boosted": True}}


FIELDS = ["id", "title", "artist_title", "date_display", "image_id",
          "artwork_type_title", "thumbnail"]

#: What can go wrong asking a source for a work: the network, and an
#: answer that is not the shape it was last time. Either way the source
#: is skipped this turn rather than the sender stopping.
FAILED = (urllib.error.URLError, OSError, ValueError, KeyError, TypeError,
          IndexError)


class Work:
    """One picture and what the label under it should say.

    `artist` is the credit line, whoever that is for the source: the
    painter, NASA and the mission, the studio. `url` is where the image
    is and `key` the name it is kept under.
    """

    def __init__(self, id, title, artist, date="", url=None, key=None,
                 aspect=None):
        self.id = id
        self.title = (title or "Untitled").strip()
        self.artist = (artist or "Unknown").strip()
        self.date = (date or "").strip()
        self.url = url
        self.key = key
        #: Filled in by the gallery once the work has been chosen and its
        #: image is on disk, because neither is known from the search.
        self.path = None
        self.series = None
        #: The work's own proportions, so a page can shape a box for it and
        #: the renderer knows how much of its area will be bars. A source
        #: that does not say is measured from the file once it is down.
        self.aspect = aspect
        #: Whether the credit is the licence's condition rather than a
        #: courtesy, so a label that would have to cut it short is not one
        #: to use.
        self.attributed = False

    @property
    def series_of(self):
        """A print's series, which its title carries after a comma.

        Half the Japanese titles here are `Place, from the series "..."`,
        and the place is the picture while the series is the shelf it came
        off. Splitting them lets the label lead with the one and dim the
        other.
        """
        for mark in (', from the series ', ', from the album '):
            if mark in self.title:
                head, _, tail = self.title.partition(mark)
                return head.strip(), tail.strip().strip('"')
        return self.title, ""

    def __repr__(self):
        return f"<Work {self.id} {self.artist}: {self.title[:40]}>"


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return fh.read()


def _post(query, page, size):
    body = json.dumps({"query": query, "fields": FIELDS,
                       "limit": size, "page": page}).encode()
    req = urllib.request.Request(
        API, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": AGENT, "AIC-User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=20) as fh:
        return json.load(fh)


def _pool(items, seen):
    """What has not been up yet, or everything when all of it has.

    Forgetting is better than refusing: a long run should come round
    again rather than stop having anything to show.
    """
    fresh = [i for i in items if i.id not in seen]
    if not fresh:
        seen.difference_update(i.id for i in items)
    return fresh or items


class Museum:
    """A series from the Art Institute of Chicago: one search."""

    name = "the museum"

    def __init__(self, query):
        self.query = query

    def pick(self, rng, seen):
        total = _post(self.query, 1, 1)["pagination"]["total"]
        if not total:
            return None
        pages = max(1, min(DEPTH, total) // PAGE)
        got = _post(self.query, rng.randint(1, pages), PAGE)
        works = [self._work(w) for w in got.get("data", ())
                 if w.get("image_id")]
        return rng.choice(_pool(works, seen)) if works else None

    @staticmethod
    def _work(raw):
        thumb = raw.get("thumbnail") or {}
        w, h = thumb.get("width") or 0, thumb.get("height") or 0
        return Work(raw.get("id"), raw.get("title"), raw.get("artist_title"),
                    raw.get("date_display"),
                    url=IIIF % (raw["image_id"], WIDTH),
                    key=raw["image_id"],
                    aspect=(w / h) if w and h else 1.4)


NASA = "https://images-api.nasa.gov/search"

#: Words in a NASA title or description that mean the picture is a
#: drawing of a thing rather than the thing, or a chart about it. The
#: library is mostly press images, and a press image of a mission not yet
#: flown is an artist's impression with a caption in it.
NOT_A_PHOTO = re.compile(
    r"\b(illustrations?|artist|concepts?|diagrams?|charts?|graphs?|plots?|"
    r"maps?|posters?|logos?|infographics?|animations?|renderings?|"
    r"simulations?|models?|spectr(um|a)|annotated|labell?ed|patch|"
    r"emblem|wavelengths?|panels?|side by side|collage|before and after|"
    r"movie|video)\b", re.I)

#: Words that mean the picture is of people or of hardware on the ground:
#: a launch, a clean room, an award. The searches below find the sky, but
#: every search in the library brings some of these with it.
ON_THE_GROUND = re.compile(
    r"\b(receives?|award|ceremony|briefing|conference|press|team|"
    r"engineers?|technicians?|workers?|clean ?room|processing|launch(es|ed)?|"
    r"liftoff|rollout|pad|hangar|facility|lab|laboratory|mockup|test|"
    r"visit(s|ors)?|students?|portrait)\b", re.I)

#: The centres whose pictures are of people and rockets rather than of
#: space: Kennedy's are launches, Headquarters' are events.
GROUND_CENTRES = {"KSC", "HQ"}

#: The spacecraft and observatories a credit is worth naming, looked for
#: in this order in a picture's title, keywords and description. The first
#: found is the mission on the label; one with none is credited to the
#: NASA centre that published it.
MISSIONS = (
    ("James Webb", "Webb"), ("JWST", "Webb"), ("Webb", "Webb"),
    ("Hubble", "Hubble"), ("Spitzer", "Spitzer"), ("Chandra", "Chandra"),
    ("WISE", "WISE"), ("Kepler", "Kepler"), ("Cassini", "Cassini"),
    ("Juno", "Juno"), ("Voyager", "Voyager"), ("Galileo", "Galileo"),
    ("New Horizons", "New Horizons"), ("MESSENGER", "MESSENGER"),
    ("Dawn", "Dawn"), ("Curiosity", "Curiosity"),
    ("Perseverance", "Perseverance"), ("Opportunity", "Opportunity"),
    ("Spirit", "Spirit"), ("Mars Reconnaissance", "MRO"),
    ("Solar Dynamics", "SDO"), ("SDO", "SDO"), ("Apollo", "Apollo"),
    ("International Space Station", "ISS"), ("Expedition", "ISS"),
    ("Space Shuttle", "Shuttle"), ("STS-", "Shuttle"), ("Terra", "Terra"),
    ("Aqua", "Aqua"), ("Landsat", "Landsat"), ("Suomi", "Suomi NPP"),
    ("GOES", "GOES"), ("Blue Marble", "Blue Marble"),
)

#: A picture much wider or taller than this is a panorama or a strip, and
#: on a wall it is a line with black above and below it.
SHAPE = (0.5, 2.6)


class Nasa:
    """A series from NASA's image library: a handful of searches.

    Each turn takes one search at random and a random page of it. The
    library is public domain and asks for no key; its images come in
    sizes, and the medium one, about 1280 across, is the most any wall
    can use.
    """

    name = "NASA"

    def __init__(self, *queries):
        self.queries = queries

    def pick(self, rng, seen):
        q = rng.choice(self.queries)
        args = {"q": q, "media_type": "image", "page_size": PAGE}
        head = self._search(dict(args, page_size=1))
        total = head["collection"]["metadata"]["total_hits"]
        if not total:
            return None
        pages = max(1, min(DEPTH, total) // PAGE)
        got = self._search(dict(args, page=rng.randint(1, pages)))
        works = [w for w in map(self._work, got["collection"]["items"]) if w]
        return rng.choice(_pool(works, seen)) if works else None

    @staticmethod
    def _search(args):
        return json.loads(_get(NASA + "?" + urllib.parse.urlencode(args)))

    @staticmethod
    def _work(item):
        """A search hit as a work, or None if it is not a photograph that
        will hang: a drawing, a chart, a strip, or no image to fetch."""
        data = (item.get("data") or [{}])[0]
        title = data.get("title") or ""
        words = (title, " ".join(data.get("keywords") or ()),
                 data.get("description") or "")
        said = title + " " + (data.get("description") or "")[:300]
        if (data.get("media_type") != "image" or NOT_A_PHOTO.search(said)
                or ON_THE_GROUND.search(said)
                or data.get("center") in GROUND_CENTRES
                or " " not in title.strip()):
            # A title with no space in it is a frame number, which is how
            # the raw photography is filed: never the picture chosen to
            # be shown, and nothing to put on a label.
            return None
        best = None
        for link in item.get("links") or ():
            href = link.get("href") or ""
            if link.get("render") != "image" or not href.endswith(".jpg"):
                continue
            if "~medium." in href or (best is None and "~small." in href):
                best = link
        if best is None or not best.get("width") or not best.get("height"):
            return None
        aspect = best["width"] / best["height"]
        if not SHAPE[0] <= aspect <= SHAPE[1]:
            return None
        # The title first, then the keywords, then the description: a
        # shuttle photograph whose description mentions Voyager is the
        # shuttle's.
        mission = next((short for text in words for long, short in MISSIONS
                        if long.lower() in text.lower()), None)
        credit = f"NASA/{mission}" if mission else \
            f"NASA/{data.get('center') or 'NASA'}".replace("NASA/NASA", "NASA")
        nasa_id = data.get("nasa_id") or href
        # The credit already says NASA, and a label has no room to say
        # it twice.
        title = re.sub(r"^NASA(['’]s)?\s+", "", title.strip())
        return Work(f"nasa:{nasa_id}", title, credit,
                    (data.get("date_created") or "")[:4],
                    url=best["href"], key="nasa-" + re.sub(
                        r"[^A-Za-z0-9_.-]", "_", nasa_id),
                    aspect=aspect)


class Numbered:
    """A numbered set of stills: a still is a set and a number, so nothing
    need be listed. `url` is a template with `{set}` and `{n}` in it,
    `sets` names each set as (title, year), and every set has `count`
    stills, numbered from 1. The series' `name` is the credit."""

    def __init__(self, series, name, url, sets, count):
        self.series, self.name, self.url = series, name, url
        self.sets = {k: tuple(v) for k, v in sets.items()}
        self.count = int(count)

    def pick(self, rng, seen):
        for _ in range(8):
            one = rng.choice(list(self.sets))
            n = rng.randint(1, self.count)
            if f"{self.series}:{one}{n:03d}" not in seen:
                break
        title, year = (self.sets[one] + ("", ""))[:2]
        return Work(f"{self.series}:{one}{n:03d}", title, self.name, year,
                    url=self.url.format(set=one, n=n),
                    key=f"{self.series}-{one}{n:03d}")


REVOY = "https://www.peppercarrot.com/0_sources/"

#: Where David Revoy keeps his finished pictures, the wallpapers before
#: the artworks: the same painting is often in both, and the wallpaper is
#: the one framed for a screen.
SHELVES = ("0ther/wallpapers/low-res/", "0ther/artworks/low-res/")

#: Pictures that are about the comic rather than of it -- covers with
#: lettering on them, reference sheets, thank-you cards -- and would hang
#: as a page of text.
NOT_A_PAINTING = re.compile(
    r"(cover|manual|system|merci|thank|concept|design|clothes|signing|"
    r"first-artwork)", re.I)


class PepperCarrot:
    """A painting from Pepper&Carrot, credited to David Revoy (CC-BY).

    The site has no search, only its folders, so the list of pictures is
    read from their index once and kept. Episode numbers are named from
    the episode list, so a wallpaper made from an episode is labelled
    with the episode's title.
    """

    name = "Pepper&Carrot"

    def __init__(self):
        self.works = []
        self.episodes = {}

    def pick(self, rng, seen):
        if not self.works:
            self.works = self._list()
        return rng.choice(_pool(self.works, seen)) if self.works else None

    def _list(self):
        try:
            for ep in json.loads(_get(REVOY + "episodes.json")):
                m = re.match(r"ep(\d+)_(.*)", ep.get("name") or "")
                if m:
                    self.episodes[int(m.group(1))] = \
                        m.group(2).replace("-", " ")
        except FAILED:
            pass                    # the number alone will do for a label
        works, named = [], set()
        for shelf in SHELVES:
            page = _get(REVOY + shelf).decode("utf-8", "replace")
            for f in re.findall(r'href="([^"?/]+\.jpg)"', page):
                f = html.unescape(urllib.parse.unquote(f))
                m = re.match(r"(\d{4})-\d\d-\d\d_(.+?)_by-David-Revoy\.jpg$",
                             f)
                if not m or NOT_A_PAINTING.search(m.group(2)):
                    continue
                if m.group(2).lower() in named:
                    continue
                named.add(m.group(2).lower())
                works.append(self._work(shelf, f, m.group(1), m.group(2)))
        return works

    def _work(self, shelf, file, year, slug):
        words = re.sub(r"-s-", "'s ", slug).replace("_", " ").replace("-", " ")
        when = year
        ep = re.match(r"Episode (\d+)\s*(.*)", words)
        if ep:
            number = int(ep.group(1))
            rest = re.sub(r"\bpage \d+\b|\blandscape\b", "", ep.group(2))
            words = rest.strip() or self.episodes.get(number) \
                or f"Episode {number}"
            when = f"episode {number}"
        title = words[:1].upper() + words[1:]
        work = Work(f"peppercarrot:{file}", title,
                    "Pepper&Carrot - David Revoy", when,
                    url=REVOY + shelf + urllib.parse.quote(file),
                    key="peppercarrot-" + file[:-4])
        work.attributed = True              # CC-BY
        return work


def _jpeg_size(path):
    """A JPEG's (width, height) from its frame header, or None.

    Enough of the format to find the one marker that carries the size,
    so a work whose source does not say its shape can be measured
    without decoding it.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 18)
    except OSError:
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h) if w and h else None
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


#: What to show, and why it suits a grid of characters. The note is the
#: reason the series is here at all, and is what to weigh if one of them
#: ever has to go.
SERIES = {
    "famous": (
        Museum(_q(NOTABLE, PAINTING)),
        "the hundred-odd paintings the museum itself marks as notable: "
        "the Grande Jatte, Van Gogh's bedroom, Paris Street; Rainy Day"),
    "vangogh": (
        Museum(_by("Vincent van Gogh", PAINTING)),
        "ten paintings, and the one palette in the collection that "
        "survives being cut to two colours a cell"),
    "monet": (
        Museum(_by("Claude Monet", PAINTING)),
        "thirty-three, the haystacks and the water lilies among them: "
        "one subject painted over and over in changing light"),
    "hiroshige": (
        Museum(_by("Utagawa Hiroshige")),
        "flat inked areas inside a drawn edge, and the largest single "
        "body of work here: landscape reduced to shape already"),
    "hokusai": (
        Museum(_by("Katsushika Hokusai")),
        "the same flat colour with a harder line, and waves and mountains "
        "that are legible at any size"),
    "utamaro": (
        Museum(_by("Kitagawa Utamaro")),
        "figures drawn almost entirely in outline, which is the one mark "
        "a small grid keeps"),
    "ukiyoe": (
        Museum(_kind("woodblock print")),
        "every Japanese print the museum has open, the widest series that "
        "is still all of one medium"),
    "posada": (
        Museum(_by("Jose Guadalupe Posada")),
        "relief prints in black on bare paper: nothing but shape, and the "
        "highest contrast in the collection"),
    "lautrec": (
        Museum(_by("Henri de Toulouse-Lautrec")),
        "posters built from a few flat colours, made to read across a "
        "street and so across a wall"),
    "durer": (
        Museum(_by("Albrecht Durer")),
        "engraving, where the tone is made of lines the grid cannot hold, "
        "so what arrives is the composition"),
    "impression": (
        Museum(_anyone(["Claude Monet", "Pierre-Auguste Renoir",
                        "Edgar Degas", "Mary Cassatt", "Berthe Morisot",
                        "Camille Pissarro", "Gustave Caillebotte",
                        "Alfred Sisley", "Georges Seurat"], PAINTING)),
        "named painter by painter rather than by style, because the "
        "museum's style field is sparse and a name is never wrong"),
    "deepspace": (
        Nasa("nebula", "hubble galaxy", "spiral galaxy", "star cluster",
             "planetary nebula"),
        "nebulae, galaxies and clusters, mostly Hubble's: colour on "
        "black, and a shape that survives any reduction"),
    "planets": (
        Nasa("jupiter juno", "saturn cassini", "pluto new horizons",
             "mars rover panorama", "great red spot"),
        "the planets from the spacecraft that went there, a disc or a "
        "horizon, which is the one picture everyone knows how to read"),
    "earth": (
        Nasa("earth limb", "aurora from space station", "blue marble",
             "hurricane from space station"),
        "the Earth from orbit: the blue curve, the aurora, a hurricane"),
    "peppercarrot": (
        PepperCarrot(),
        "David Revoy's paintings for his webcomic, free to show with his "
        "name on them, bright and made of big shapes"),
}

#: Shown when no series is named. It leads with what a viewer will know
#: on sight and keeps the prints that take the reduction best, because a
#: rotation of only the technically suited would be a rotation of work
#: nobody recognises.
DEFAULT = ("famous", "impression", "hiroshige", "lautrec", "hokusai")

#: What a person chooses between: series that belong on one wall
#: together, and what each is. `art` is the default rotation;
#: `everything` is every series, each picture lettered as its own is.
CATEGORIES = {
    "art": (DEFAULT, "the art: paintings people know and Japanese prints"),
    "popular": (("famous", "impression", "lautrec"),
                "the paintings people know, and Lautrec's posters"),
    "japanese": (("hiroshige", "hokusai"), "woodblock prints"),
    "space": (("deepspace", "planets", "earth"),
              "NASA: deep space, the planets and the Earth from orbit"),
    "anime": (("peppercarrot",), "Pepper&Carrot's paintings"),
}

#: The host's own series and categories, `Numbered` sets kept as data.
OWN = os.path.expanduser("~/.bitwall/picture.json")
#: The sources a host's file may name, by what it calls them.
SOURCES = {"numbered": Numbered}


def _own(path):
    """Add the series and categories kept in `path` to SERIES and
    CATEGORIES. A file that is missing adds nothing; one that is wrong
    says why on stderr and adds what of it is right."""
    try:
        with open(path) as fh:
            got = json.load(fh)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"picture: {path}: {e}", file=sys.stderr, flush=True)
        return
    for name, spec in (got.get("series") or {}).items():
        spec = dict(spec)
        kind = spec.pop("source", "numbered")
        why = spec.pop("why", "")
        try:
            SERIES[name] = (SOURCES[kind](name, **spec), why)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            print(f"picture: {path}: series {name!r}: {e!r}, left out",
                  file=sys.stderr, flush=True)
    for name, spec in (got.get("categories") or {}).items():
        names = tuple(n for n in spec.get("series", ()) if n in SERIES)
        if names:
            CATEGORIES[name] = (names, spec.get("what", ""))


_own(os.environ.get("BITWALL_PICTURE") or OWN)
CATEGORIES["everything"] = (
    tuple(dict.fromkeys(n for names, _ in CATEGORIES.values() for n in names)),
    "all of them: art, space and the rest in one rotation")


class Gallery:
    """A rotation through named series, one work at a time.

    `next()` is the whole surface. It returns a `Work` whose image is on
    disk, or None if nothing could be reached -- never an exception,
    because a wall is a screen in another room and a network that is down
    for a minute is not a reason to stop showing the picture already up.
    """

    def __init__(self, names=DEFAULT, seed=None, cache=CACHE):
        self.names = [n for n in names if n in SERIES] or list(DEFAULT)
        self.rng = random.Random(seed)
        self.cache = cache
        os.makedirs(self.cache, exist_ok=True)
        self.turn = 0
        self.seen = set()
        self.error = None
        self.fetched = 0
        #: Who was asked last, for a label saying who is not answering.
        self.asked = SERIES[self.names[0]][0].name

    @property
    def series(self):
        return self.names[self.turn % len(self.names)]

    def next(self):
        """The next work, its image already on disk."""
        for _ in range(6):
            name = self.series
            self.turn += 1
            work = self._pick(name)
            if work is None:
                continue
            path = self._image(work)
            if path:
                work.path = path
                work.series = name
                if work.aspect is None:
                    w, h = _jpeg_size(path) or (1.4, 1)
                    work.aspect = w / h
                self.seen.add(work.id)
                return work
        return None

    def _pick(self, name):
        """One work from a series, avoiding what has been shown already."""
        source = SERIES[name][0]
        self.asked = source.name
        try:
            work = source.pick(self.rng, self.seen)
        except FAILED as e:
            self.error = f"{name}: {e}"
            return None
        if work is not None:
            self.error = None
        return work

    def _image(self, work):
        """The file for this work, downloading it the first time."""
        path = os.path.join(self.cache, f"{work.key}.jpg")
        if os.path.exists(path) and os.path.getsize(path) > 1024:
            return path
        req = urllib.request.Request(work.url,
                                     headers={"User-Agent": AGENT,
                                              "AIC-User-Agent": AGENT})
        part = path + ".part"
        try:
            with urllib.request.urlopen(req, timeout=30) as fh:
                data = fh.read()
            if len(data) < 1024:
                raise ValueError("truncated")
            with open(part, "wb") as out:
                out.write(data)
            os.replace(part, path)          # never a half file under the name
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.error = f"{work.key}: {e}"
            try:
                os.remove(part)
            except OSError:
                pass
            return None
        self.fetched += 1
        return path


def listing():
    """The categories, the series and the reason each one is here, for
    `--list`."""
    out = []
    wide = max(len(n) for n in CATEGORIES)
    for name, (names, what) in CATEGORIES.items():
        out.append(f"   {name:<{wide}}  {what}: {', '.join(names)}")
    out.append("")
    wide = max(len(n) for n in SERIES)
    for name, (_, why) in SERIES.items():
        mark = "*" if name in DEFAULT else " "
        out.append(f" {mark} {name:<{wide}}  {why}")
    out.append("")
    out.append(" * in the default rotation")
    return "\n".join(out)
