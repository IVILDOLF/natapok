#!/usr/bin/env python3
"""
kanata_render.py - draw the layers of a kanata .kbd config as SVG and/or PNG.

    python3 kanata_render.py kanata.kbd                 # -> ./keyboard_scheme/svg/*.svg
    python3 kanata_render.py kanata.kbd -f both         # svg + png
    python3 kanata_render.py kanata.kbd -o out --cols 1 # one column combined sheet

Produces one image per deflayer plus a combined sheet with all of them.
Files land in <outdir>/svg/ and <outdir>/png/ respectively:

    keyboard_scheme/
      svg/  layer_base.svg  layer_nav.svg  ...  all_layers.svg
      png/  layer_base.png  layer_nav.png  ...  all_layers.png

SVG needs nothing but the standard library. PNG needs Pillow
(`pip install pillow`); if Pillow is missing the script still writes the SVGs
and tells you so.

Only the rows present in `defsrc` are drawn - function row, number row, arrow
cluster and numpad are left out, exactly like they are left out of the config.

----------------------------------------------------------------------------
Tweaking the labels
----------------------------------------------------------------------------
Labels are worked out automatically from defalias / defvirtualkeys, which gets
the common cases right (tap-dance, tap-hold, one-shot, layer moves, S-/C-/A-
chords). Anything that comes out ugly can be pinned by hand in OVERRIDES
below: "alias name" -> ("main label", "small label under it").
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from xml.sax.saxutils import escape as xml_escape

# ---------------------------------------------------------------------------
# hand-written labels: alias name -> (main, sub).  Edit freely.
# ---------------------------------------------------------------------------
OVERRIDES = {
    "sft":  ("Shift", "one-shot · Alt=lang"),
    "osft": ("Shift", "one-shot"),
    "ctl":  ("Ctrl",  "2\u00d7 \u2192 nav"),
    "nctl": ("Ctrl",  "2\u00d7 \u2192 exit"),
    "apo":  ("'",     "2\u00d7 \u2192 sym"),
    "sapo": ("\\",    "2\u00d7 \u2192 exit"),
    "sctl": ("Ctrl",  "tap \u2192 exit"),
    "lyt":  ("[",     "2\u00d7 \u2192 APT"),
    "lytA": ("[",     "2\u00d7 \u2192 base"),
    "cma":  (",",     "2\u00d7 \u2192 Tab"),
    "dot":  (".",     "2\u00d7 \u2192 Alt"),
    "cpy":  ("Copy",  "Ctrl+C"),
    "pst":  ("Paste", "Ctrl+V"),
    "fnl":  ("Fn",    "hold \u2192 fn"),
    "bse":  ("Exit",  "\u2192 base"),
    "gnav": ("Nav",   "\u2192 nav"),
    "gsym": ("Sym",   "\u2192 sym"),
    "lang": ("Lang",  "toggle flag"),
}

# Which layer a transparent `_` falls through to.  Anything not listed falls
# through to the first layer in the file.
FALLBACK = {
    "fn": "nav",
}

# ---------------------------------------------------------------------------
# key-name -> printed label
# ---------------------------------------------------------------------------
KEYNAMES = {
    "grv": "`", "min": "-", "eql": "=", "bspc": "Bksp", "tab": "Tab",
    "caps": "Caps", "ret": "Enter", "esc": "Esc", "spc": "Space",
    "lsft": "Shift", "rsft": "Shift", "lctl": "Ctrl", "rctl": "Ctrl",
    "lalt": "Alt", "ralt": "AltGr", "lmet": "Super", "rmet": "Super",
    "cmp": "Menu", "del": "Del", "ins": "Ins",
    "up": "\u2191", "down": "\u2193", "left": "\u2190", "rght": "\u2192",
    "home": "Home", "end": "End", "pgup": "PgUp", "pgdn": "PgDn",
    "prnt": "PrtSc", "slck": "ScrLk", "pause": "Pause",
    "nlck": "NumLk", "brdn": "Bright-", "brup": "Bright+",
    "volu": "Vol+", "vold": "Vol-", "mute": "Mute",
    "XX": "\u2715", "\u2205": "\u2715", "nop0": "flag0", "nop1": "flag1",
}
for _i in range(1, 25):
    KEYNAMES["f%d" % _i] = "F%d" % _i

SHIFTED = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&",
    "8": "*", "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}",
    "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?",
    "grv": "~", "min": "_", "eql": "+",
}

MODNAMES = {"S": "Shift", "C": "Ctrl", "A": "Alt", "M": "Super",
            "AG": "AltGr", "RA": "AltGr"}

# key width in units, by defsrc name; anything else is 1u
WIDTHS = {
    "tab": 1.5, "\\": 1.5, "caps": 1.75, "ret": 2.25,
    "lsft": 2.25, "rsft": 2.75, "spc": 6.25,
    "lctl": 1.25, "lmet": 1.25, "lalt": 1.25,
    "ralt": 1.25, "rmet": 1.25, "cmp": 1.25, "rctl": 1.25,
}

HOMING = {"f", "j"}

# ---------------------------------------------------------------------------
# palette
# ---------------------------------------------------------------------------
THEME = {
    "bg":          "#f7f7f5",
    "panel":       "#ffffff",
    "key":         "#ffffff",
    "key_mod":     "#eceef2",
    "key_layer":   "#fdf1dc",
    "key_dead":    "#f0efed",
    "key_ghost":   "#fbfbfa",
    "edge":        "#b9bcc2",
    "edge_layer":  "#d8a23b",
    "edge_ghost":  "#e2e2e0",
    "text":        "#1d1f22",
    "text_sub":    "#7b7f87",
    "text_ghost":  "#b6b9be",
    "title":       "#1d1f22",
    "subtitle":    "#6b6f77",
}

# ===========================================================================
# 1. parsing
# ===========================================================================
TOKEN_RE = re.compile(r"\(|\)|[^\s()]+")


def strip_comments(text: str) -> str:
    """Remove #| block |# and ;; line comments, keeping line breaks intact."""
    text = re.sub(r"#\|.*?\|#", lambda m: "\n" * m.group(0).count("\n"),
                  text, flags=re.S)
    out = []
    for line in text.split("\n"):
        cut = line.find(";;")
        out.append(line if cut < 0 else line[:cut])
    return "\n".join(out)


def tokenize(text: str):
    """-> list of (token, line_number)."""
    toks = []
    for lineno, line in enumerate(text.split("\n")):
        for m in TOKEN_RE.finditer(line):
            toks.append((m.group(0), lineno))
    return toks


def parse_forms(toks):
    """Parse a token stream into nested lists. Atoms stay strings."""
    forms, stack = [], []
    for tok, _ in toks:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if not stack:
                raise SyntaxError("unbalanced ) in config")
            done = stack.pop()
            (stack[-1] if stack else forms).append(done)
        else:
            (stack[-1] if stack else forms).append(tok)
    if stack:
        raise SyntaxError("unbalanced ( in config")
    return forms


def parse_rows(toks, skip=0):
    """Group tokens into rows by source line, collapsing nested lists."""
    items, stack, start_line = [], [], None
    for tok, lineno in toks:
        if tok == "(":
            if not stack:
                start_line = lineno
            stack.append([])
        elif tok == ")":
            done = stack.pop()
            if stack:
                stack[-1].append(done)
            else:
                items.append((start_line, done))
        elif stack:
            stack[-1].append(tok)
        else:
            items.append((lineno, tok))
    items = items[skip:]
    rows, cur, cur_line = [], [], None
    for lineno, item in items:
        if cur_line is None or lineno == cur_line:
            cur.append(item)
        else:
            rows.append(cur)
            cur = [item]
        cur_line = lineno
    if cur:
        rows.append(cur)
    return [r for r in rows if r]


class Config:
    def __init__(self, path):
        raw = open(path, encoding="utf-8").read()
        self.raw_lines = raw.split("\n")
        body = strip_comments(raw)

        self.aliases, self.vkeys, self.vars = {}, {}, {}
        self.layers = {}          # name -> flat list of actions
        self.src_rows = []        # rows of defsrc key names
        self.comments = {}        # layer name -> comment above it

        for form in parse_forms(tokenize(body)):
            if not isinstance(form, list) or not form:
                continue
            head = form[0]
            if head == "defalias":
                rest = form[1:]
                for i in range(0, len(rest) - 1, 2):
                    self.aliases[rest[i]] = rest[i + 1]
            elif head == "defvirtualkeys" or head == "deffakekeys":
                rest = form[1:]
                for i in range(0, len(rest) - 1, 2):
                    self.vkeys[rest[i]] = rest[i + 1]
            elif head == "defvar":
                rest = form[1:]
                for i in range(0, len(rest) - 1, 2):
                    self.vars[rest[i]] = rest[i + 1]

        # defsrc / deflayer need the source line breaks, so re-read the spans
        for head, name, inner in self._spans(body):
            toks = tokenize(inner)
            if head == "defsrc":
                self.src_rows = [[t for t in row] for row in parse_rows(toks)]
            else:
                rows = parse_rows(toks, skip=1)   # drop the layer name token
                self.layers[name] = [k for row in rows for k in row]
                self.comments[name] = self._comment_above(name)

        if not self.src_rows:
            raise SystemExit("no (defsrc ...) found in the config")

        self.row_lengths = [len(r) for r in self.src_rows]
        self.src_flat = [k for r in self.src_rows for k in r]

        for name, keys in list(self.layers.items()):
            if len(keys) != len(self.src_flat):
                print("  ! layer '%s' has %d keys, defsrc has %d - skipped"
                      % (name, len(keys), len(self.src_flat)), file=sys.stderr)
                del self.layers[name]

    @staticmethod
    def _spans(body):
        """Yield (head, name, inner text) for defsrc and deflayer forms."""
        for m in re.finditer(r"\((defsrc|deflayer)\b", body):
            depth, i = 0, m.start()
            while i < len(body):
                if body[i] == "(":
                    depth += 1
                elif body[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            inner = body[m.end():i]
            head = m.group(1)
            name = "defsrc"
            if head == "deflayer":
                nm = re.search(r"[^\s()]+", inner)
                name = nm.group(0) if nm else "?"
            yield head, name, inner

    def _comment_above(self, layer):
        """The ;; comment block sitting right above (deflayer <layer>."""
        pat = re.compile(r"^\s*\(deflayer\s+%s\b" % re.escape(layer))
        idx = next((i for i, l in enumerate(self.raw_lines) if pat.match(l)), None)
        if idx is None:
            return ""
        out, i = [], idx - 1
        while i >= 0 and self.raw_lines[i].strip().startswith(";;"):
            line = self.raw_lines[i].strip().lstrip(";").strip()
            if line and set(line) - set("-=_ "):
                out.append(line)
            i -= 1
        return " ".join(reversed(out))

    def rows_of(self, layer):
        """Layer actions re-chunked into the defsrc row shape."""
        keys, rows, pos = self.layers[layer], [], 0
        for n in self.row_lengths:
            rows.append(keys[pos:pos + n])
            pos += n
        return rows


# ===========================================================================
# 2. labels
# ===========================================================================
def pretty_atom(tok: str) -> str:
    if tok in KEYNAMES:
        return KEYNAMES[tok]
    if re.fullmatch(r"[A-Za-z0-9]", tok):
        return tok.upper()
    m = re.fullmatch(r"([SCAM]|AG|RA)-(.+)", tok)
    if m:
        mod, rest = m.group(1), m.group(2)
        if mod == "S":
            base = SHIFTED.get(rest)
            if base:
                return base
            if re.fullmatch(r"[A-Za-z]", rest):
                return rest.upper()
        return "%s+%s" % (MODNAMES.get(mod, mod), pretty_atom(rest))
    return tok


class Labeller:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def key(self, action):
        """-> (main, sub) for one entry of a deflayer."""
        if isinstance(action, str):
            if action.startswith("@"):
                name = action[1:]
                if name in OVERRIDES:
                    return OVERRIDES[name]
        return self.act(action, set())

    def act(self, a, seen, depth=0):
        if depth > 8:
            return ("\u2026", "")
        if isinstance(a, str):
            if a.startswith("@"):
                name = a[1:]
                if name in OVERRIDES:
                    return OVERRIDES[name]
                if name in seen:
                    return (name, "")
                if name in self.cfg.aliases:
                    return self.act(self.cfg.aliases[name], seen | {name}, depth + 1)
                return ("@" + name, "")
            if a.startswith("$"):
                return (self.cfg.vars.get(a[1:], a), "")
            return (pretty_atom(a), "")

        if not a:
            return ("", "")
        head = a[0]

        if head in ("tap-dance", "tap-dance-eager"):
            inner = a[2] if len(a) > 2 and isinstance(a[2], list) else []
            if len(inner) >= 2:
                first = self.act(inner[0], seen, depth + 1)[0]
                rest = " / ".join(self.act(x, seen, depth + 1)[0] for x in inner[1:])
                return (first, "2\u00d7 %s" % rest)
            return ("tap-dance", "")

        if head.startswith("tap-hold"):
            if len(a) >= 5:
                tap = self.act(a[3], seen, depth + 1)[0]
                hold = self.act(a[4], seen, depth + 1)[0]
                return (tap, "hold: %s" % hold)
            return ("tap-hold", "")

        if head.startswith("one-shot"):
            if head == "one-shot-pause-processing":
                return ("", "")
            if len(a) >= 3:
                return (self.act(a[2], seen, depth + 1)[0], "one-shot")
            return ("one-shot", "")

        if head in ("layer-while-held", "layer-toggle"):
            return (a[1], "hold layer")
        if head in ("layer-switch", "layer-add"):
            return (a[1], "set layer")

        if head in ("multi", "macro"):
            mains, subs = [], []
            for part in a[1:]:
                m, s = self.act(part, seen, depth + 1)
                if m:
                    mains.append(m)
                if s:
                    subs.append(s)
            mains = _dedupe(mains)
            # a multi that only releases overlays is an "exit"
            if mains and all(x.startswith("exit ") for x in mains):
                return ("Exit", "\u2192 base")
            enter = [x for x in mains if not x.startswith("exit ")]
            if enter and len(enter) < len(mains):
                return (enter[0], "\u2192 layer")
            return (" + ".join(mains[:2]), _dedupe(subs)[0] if subs else "")

        if head in ("on-press", "on-release"):
            return self.act(a[1:] if len(a) > 2 else a[1], seen, depth + 1)

        if head in ("press-virtualkey", "press-vkey", "release-virtualkey",
                    "release-vkey", "tap-virtualkey", "tap-vkey",
                    "on-press-fakekey", "toggle-virtualkey"):
            target = a[-1]
            body = self.cfg.vkeys.get(target)
            if body is None:
                return (target, "virtual key")
            label = self.act(body, seen | {target}, depth + 1)[0]
            if "release" in head:
                return ("exit %s" % label, "")
            return (label, "")

        if head == "switch":
            # summarise the default () branch, else the first one
            branches = a[1:]
            chosen = None
            for i in range(0, len(branches) - 1, 3):
                cond, action = branches[i], branches[i + 1]
                if chosen is None:
                    chosen = action
                if isinstance(cond, list) and not cond:
                    chosen = action
                    break
            if chosen is not None:
                return self.act(chosen, seen, depth + 1)
            return ("switch", "")

        if head == "unmod" or head == "unshift":
            return (self.act(a[-1], seen, depth + 1)[0], head)

        return (self.act(head, seen, depth + 1)[0], "")


def _dedupe(items):
    out = []
    for x in items:
        if x and x not in out:
            out.append(x)
    return out


# ===========================================================================
# 3. drawing backends
# ===========================================================================
class Canvas:
    """Minimal drawing surface: rounded rects, text, lines."""

    def __init__(self, w, h, bg):
        self.w, self.h, self.bg = w, h, bg

    def rect(self, x, y, w, h, r=6, fill="#fff", stroke=None, sw=1.0, dash=False):
        raise NotImplementedError

    def text(self, x, y, s, size=13, fill="#000", anchor="middle",
             bold=False, italic=False):
        raise NotImplementedError

    def line(self, x1, y1, x2, y2, stroke="#000", sw=1.0):
        raise NotImplementedError

    def measure(self, s, size, bold=False):
        raise NotImplementedError

    def save(self, path):
        raise NotImplementedError


FONT_STACK = "DejaVu Sans, Segoe UI, Helvetica Neue, Arial, sans-serif"


class SVGCanvas(Canvas):
    def __init__(self, w, h, bg):
        super().__init__(w, h, bg)
        self.parts = []

    def rect(self, x, y, w, h, r=6, fill="#fff", stroke=None, sw=1.0, dash=False):
        s = ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%.1f" '
             'fill="%s"' % (x, y, w, h, r, fill))
        if stroke:
            s += ' stroke="%s" stroke-width="%.2f"' % (stroke, sw)
            if dash:
                s += ' stroke-dasharray="3 3"'
        self.parts.append(s + "/>")

    def text(self, x, y, s, size=13, fill="#000", anchor="middle",
             bold=False, italic=False):
        if not s:
            return
        self.parts.append(
            '<text x="%.1f" y="%.1f" font-family="%s" font-size="%.1f" '
            'fill="%s" text-anchor="%s"%s%s>%s</text>'
            % (x, y, FONT_STACK, size, fill, anchor,
               ' font-weight="600"' if bold else "",
               ' font-style="italic"' if italic else "",
               xml_escape(s)))

    def line(self, x1, y1, x2, y2, stroke="#000", sw=1.0):
        self.parts.append(
            '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="%.2f" stroke-linecap="round"/>'
            % (x1, y1, x2, y2, stroke, sw))

    def measure(self, s, size, bold=False):
        # rough metric: good enough to decide when to shrink a label
        narrow, wide = set("iljtfr.,;:'!|[]()"), set("mwMW@%")
        u = 0.0
        for ch in s:
            u += 0.38 if ch in narrow else 0.98 if ch in wide else 0.66
        # deliberately a little pessimistic: better a slightly small label
        # than one that runs over the edge of the key
        return u * size * (1.12 if bold else 1.04)

    def save(self, path):
        head = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
                'viewBox="0 0 %d %d">\n<rect width="100%%" height="100%%" '
                'fill="%s"/>\n' % (self.w, self.h, self.w, self.h, self.bg))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(head + "\n".join(self.parts) + "\n</svg>\n")


class PNGCanvas(Canvas):
    SCALE = 2                                  # supersampling

    FONT_PATHS = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    BOLD_PATHS = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]

    def __init__(self, w, h, bg):
        super().__init__(w, h, bg)
        from PIL import Image, ImageDraw
        self.Image = Image
        self.img = Image.new("RGB", (w * self.SCALE, h * self.SCALE), bg)
        self.d = ImageDraw.Draw(self.img)
        self._cache = {}

    def _font(self, size, bold=False):
        key = (round(size * self.SCALE), bold)
        if key not in self._cache:
            from PIL import ImageFont
            font = None
            for p in (self.BOLD_PATHS if bold else self.FONT_PATHS):
                if os.path.exists(p):
                    try:
                        font = ImageFont.truetype(p, key[0])
                        break
                    except Exception:
                        pass
            if font is None:
                try:
                    font = ImageFont.load_default(size=key[0])
                except TypeError:
                    font = ImageFont.load_default()
            self._cache[key] = font
        return self._cache[key]

    def rect(self, x, y, w, h, r=6, fill="#fff", stroke=None, sw=1.0, dash=False):
        s = self.SCALE
        self.d.rounded_rectangle(
            [x * s, y * s, (x + w) * s, (y + h) * s], radius=r * s,
            fill=fill, outline=stroke, width=max(1, int(round(sw * s))))

    def text(self, x, y, s_, size=13, fill="#000", anchor="middle",
             bold=False, italic=False):
        if not s_:
            return
        s = self.SCALE
        font = self._font(size, bold)
        w = self.measure(s_, size, bold)
        if anchor == "middle":
            x -= w / 2.0
        elif anchor == "end":
            x -= w
        # y is an SVG baseline; PIL draws from the ascender top
        try:
            asc, _ = font.getmetrics()
        except Exception:
            asc = size * s * 0.8
        self.d.text((x * s, y * s - asc), s_, font=font, fill=fill)

    def line(self, x1, y1, x2, y2, stroke="#000", sw=1.0):
        s = self.SCALE
        self.d.line([x1 * s, y1 * s, x2 * s, y2 * s], fill=stroke,
                    width=max(1, int(round(sw * s))))

    def measure(self, s_, size, bold=False):
        font = self._font(size, bold)
        try:
            return self.d.textlength(s_, font=font) / self.SCALE
        except Exception:
            return len(s_) * size * 0.6

    def save(self, path):
        img = self.img.resize((self.w, self.h), self.Image.LANCZOS)
        img.save(path)


# ===========================================================================
# 4. layout
# ===========================================================================
class Renderer:
    def __init__(self, cfg: Config, unit=62, gap=6):
        self.cfg = cfg
        self.lab = Labeller(cfg)
        self.unit, self.gap = unit, gap
        self.pad = 20
        self.title_h = 54
        self.units_wide = max(sum(WIDTHS.get(k, 1.0) for k in row)
                              for row in cfg.src_rows)
        self.base_layer = next(iter(cfg.layers))

    # -- sizes ------------------------------------------------------------
    @property
    def panel_w(self):
        return int(self.units_wide * self.unit + 2 * self.pad)

    @property
    def panel_h(self):
        return int(len(self.cfg.src_rows) * self.unit + self.title_h
                   + 2 * self.pad - self.gap)

    # -- transparency ------------------------------------------------------
    def resolve(self, layer, index, chain=()):
        """Follow `_` down the fallback chain. -> (action, is_ghost)."""
        act = self.cfg.rows_of(layer)[0] if False else self.cfg.layers[layer][index]
        if act != "_":
            return act, bool(chain)
        nxt = FALLBACK.get(layer, self.base_layer)
        if nxt == layer or nxt not in self.cfg.layers or nxt in chain:
            return "_", True
        return self.resolve(nxt, index, chain + (layer,))

    # -- one layer ---------------------------------------------------------
    def draw_layer(self, c: Canvas, layer, ox=0, oy=0, panel=True):
        u, gap, pad = self.unit, self.gap, self.pad
        if panel:
            c.rect(ox, oy, self.panel_w, self.panel_h, r=12,
                   fill=THEME["panel"], stroke=THEME["edge"], sw=1.0)

        c.text(ox + pad, oy + pad + 16, layer, size=21, fill=THEME["title"],
               anchor="start", bold=True)
        note = self.cfg.comments.get(layer, "")
        note = re.sub(r"^%s\s*[-\u2013]\s*" % re.escape(layer), "", note)
        if note:
            note = self._clip(c, note, self.panel_w - 2 * pad - 4, 11.5)
            c.text(ox + pad, oy + pad + 34, note, size=11.5,
                   fill=THEME["subtitle"], anchor="start")

        idx, y = 0, oy + pad + self.title_h
        for row in self.cfg.src_rows:
            x = ox + pad
            for src in row:
                w = WIDTHS.get(src, 1.0) * u
                act, ghost = self.resolve(layer, idx)
                self.draw_key(c, x, y, w - gap, u - gap, src, act, ghost)
                x += w
                idx += 1
            y += u

    def draw_key(self, c, x, y, w, h, src, act, ghost):
        main, sub = self.lab.key(act)
        if act == "_":
            main, sub = "\u00b7", ""
            ghost = True
        dead = (isinstance(act, str) and act in ("XX", "\u2205"))

        is_layer = bool(sub) and ("\u2192" in sub or "layer" in sub)
        is_mod = main in ("Shift", "Ctrl", "Alt", "AltGr", "Super", "Caps")

        fill = (THEME["key_ghost"] if ghost else
                THEME["key_dead"] if dead else
                THEME["key_layer"] if is_layer else
                THEME["key_mod"] if is_mod else THEME["key"])
        edge = (THEME["edge_ghost"] if ghost else
                THEME["edge_layer"] if is_layer else THEME["edge"])
        c.rect(x, y, w, h, r=7, fill=fill, stroke=edge,
               sw=1.4 if is_layer and not ghost else 1.0, dash=ghost)

        tcol = THEME["text_ghost"] if ghost else THEME["text"]
        scol = THEME["text_ghost"] if ghost else THEME["text_sub"]
        cx = x + w / 2.0

        avail = w - 8
        if sub:
            size = self._fit(c, main, avail, 15.5, bold=True)
            c.text(cx, y + h / 2.0 + 1, main, size=size, fill=tcol, bold=True)
            ssize = self._fit(c, sub, avail, 9.5, floor=6.0)
            c.text(cx, y + h - 7, self._clip(c, sub, avail, ssize),
                   size=ssize, fill=scol)
        else:
            size = self._fit(c, main, avail, 16.0, bold=True)
            c.text(cx, y + h / 2.0 + size * 0.36, main, size=size,
                   fill=tcol, bold=True)

        if src in HOMING and not ghost:
            c.line(cx - 7, y + h - 5.5, cx + 7, y + h - 5.5,
                   stroke=THEME["text_sub"], sw=1.2)

    # -- combined sheet ----------------------------------------------------
    TITLE = "kanata layers"

    def header_layout(self, width):
        """-> (content_top, legend_xy or None). Keeps the legend off the title."""
        m = SVGCanvas(10, 10, "#fff")
        tw = m.measure(self.TITLE, 24, True)
        lw = self.legend_width(m)
        if 24 + tw + 30 + lw <= width:
            return 52, (width - 24, 32, "end")
        if lw + 48 <= width:
            return 76, (24, 60, "start")
        return 52, None

    def draw_all(self, c, names, cols, title=None):
        gapx = gapy = 22
        c.text(gapx + 2, 34, title or self.TITLE, size=24,
               fill=THEME["title"], anchor="start", bold=True)
        top, legend = self.header_layout(c.w)
        if legend:
            self.legend(c, legend[0], legend[1], anchor=legend[2])
        for i, name in enumerate(names):
            col, row = i % cols, i // cols
            self.draw_layer(c, name,
                            ox=gapx + col * (self.panel_w + gapx),
                            oy=top + row * (self.panel_h + gapy))

    def sheet_size(self, n, cols):
        rows = (n + cols - 1) // cols
        gapx = gapy = 22
        width = int(cols * self.panel_w + (cols + 1) * gapx)
        top, _ = self.header_layout(width)
        return width, int(top + rows * self.panel_h + rows * gapy + 10)

    LEGEND = [("key_layer", "edge_layer", False, "layer / mode key"),
              ("key_ghost", "edge_ghost", True, "transparent (_), inherited"),
              ("key_mod", "edge", False, "modifier"),
              ("key_dead", "edge", False, "dead key (XX)")]

    def legend_width(self, c, size=10.5):
        w = sum(25 + c.measure(t, size) for *_, t in self.LEGEND)
        return w + 18 * (len(self.LEGEND) - 1)

    def legend(self, c, x, y, anchor="start"):
        items = [(THEME[a], THEME[b], d, t) for a, b, d, t in self.LEGEND]
        size = 10.5
        widths = [20 + 5 + c.measure(t, size) for *_, t in items]
        total = sum(widths) + 18 * (len(items) - 1)
        cx = x - total if anchor == "end" else x
        for (fill, edge, dash, text), wd in zip(items, widths):
            c.rect(cx, y - 10, 20, 13, r=4, fill=fill, stroke=edge,
                   sw=1.2, dash=dash)
            c.text(cx + 25, y, text, size=size, fill=THEME["subtitle"],
                   anchor="start")
            cx += wd + 18

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _fit(c, text, avail, start, floor=7.0, bold=False):
        size = start
        while size > floor and c.measure(text, size, bold) > avail:
            size -= 0.5
        return size

    @staticmethod
    def _clip(c, text, avail, size, bold=False):
        if c.measure(text, size, bold) <= avail:
            return text
        while text and c.measure(text + "\u2026", size, bold) > avail:
            text = text[:-1]
        return text + "\u2026"


# ===========================================================================
# 5. cli
# ===========================================================================
def make_canvas(fmt, w, h):
    if fmt == "png":
        return PNGCanvas(w, h, THEME["bg"])
    return SVGCanvas(w, h, THEME["bg"])


def slug(name: str) -> str:
    """File-safe layer name: hyphens and spaces become underscores."""
    return re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_") or "layer"


def main():
    ap = argparse.ArgumentParser(
        description="Render kanata .kbd layers to SVG/PNG.")
    ap.add_argument("config", help="path to the .kbd file")
    ap.add_argument("-o", "--outdir", default="keyboard_scheme",
                    help="output root; images go into <outdir>/svg and "
                         "<outdir>/png (default keyboard_scheme)")
    ap.add_argument("-f", "--format", choices=("svg", "png", "both"),
                    default="svg")
    ap.add_argument("--cols", type=int, default=2,
                    help="columns in the combined sheet (default 2)")
    ap.add_argument("--unit", type=int, default=62,
                    help="key pitch in px (default 62)")
    ap.add_argument("--layers", default="",
                    help="comma-separated subset of layers to draw")
    args = ap.parse_args()

    cfg = Config(args.config)
    names = list(cfg.layers)
    if args.layers:
        want = [s.strip() for s in args.layers.split(",") if s.strip()]
        missing = [w for w in want if w not in cfg.layers]
        if missing:
            raise SystemExit("no such layer(s): %s" % ", ".join(missing))
        names = want
    if not names:
        raise SystemExit("no layers found")

    r = Renderer(cfg, unit=args.unit)

    formats = ["svg", "png"] if args.format == "both" else [args.format]
    if "png" in formats:
        try:
            import PIL  # noqa: F401
        except ImportError:
            print("Pillow is not installed - writing SVG only "
                  "(pip install pillow for PNG)", file=sys.stderr)
            formats = [f for f in formats if f != "png"] or ["svg"]

    written = []
    for fmt in formats:
        subdir = os.path.join(args.outdir, fmt)
        os.makedirs(subdir, exist_ok=True)

        for name in names:
            c = make_canvas(fmt, r.panel_w, r.panel_h)
            r.draw_layer(c, name)
            path = os.path.join(subdir, "layer_%s.%s" % (slug(name), fmt))
            c.save(path)
            written.append(path)

        cols = max(1, min(args.cols, len(names)))
        w, h = r.sheet_size(len(names), cols)
        c = make_canvas(fmt, w, h)
        r.draw_all(c, names, cols)
        path = os.path.join(subdir, "all_layers.%s" % fmt)
        c.save(path)
        written.append(path)

    print("layers: %s" % ", ".join(names))
    for p in written:
        print("  wrote %s" % p)


if __name__ == "__main__":
    main()
