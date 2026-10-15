"""Parse Yahoo Player List 'Week N (proj)' PDF exports -> player list JSON
(the payload YahooWeeklyPdfAdapter reads). Week-agnostic; first used for Week 6.

usage: python3 -I scripts/parse_yahoo_weekly_playerlist.py INPUT_DIR OUT_JSON
  INPUT_DIR holds the Yahoo Player List PDFs (converted with `pdftotext -layout`
  into a temp dir, needs poppler) or already-converted .txt dumps.
  Prints diagnostics: glitched rows, dropped rows, whole-number renderings, stripped tags.
Mirrors the Week 3-5 parser behaviour: dashes -> None, status tags stripped from
names, malformed (page-break glitched) rows detected structurally and replaced by
the clean duplicate Yahoo re-prints after the form feed. Nothing is guessed.
"""
import glob
import json
import os
import re
import sys

import subprocess
import tempfile

IN_DIR, OUT = sys.argv[1], sys.argv[2]
TXT_DIR = IN_DIR
if glob.glob(os.path.join(IN_DIR, "*.pdf")):
    TXT_DIR = tempfile.mkdtemp(prefix="yahoo_txt_")
    for pdf in sorted(glob.glob(os.path.join(IN_DIR, "*.pdf"))):
        base = os.path.splitext(os.path.basename(pdf))[0]
        subprocess.run(["pdftotext", "-layout", pdf, os.path.join(TXT_DIR, base + ".txt")], check=True)

HDR = re.compile(r"^[\s\ue000-\uf8ff]*([A-Za-z]{2,3}) - (QB|RB|WR|TE)\s+(.*\S)\s*$")
NUM = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
STATUS_TAGS = {"Q", "O", "D", "IR", "IR-R", "NA", "PUP", "SUSP", "COV", "IR-NR", "PUP-R", "PUP-P", "DNR"}
# last 20 tokens: GP Bye FanPts Pre Act %Ros | PassYds PassTD Int | RushAtt RushYds RushTD |
#                 Tgt Rec RecYds RecTD | RetYds RetTD | 2PT FumLost
FIELDS = ["gp", "bye", "fan", "pre", "act", "ros", "pass_yards", "pass_tds", "interceptions",
          "rush_attempts", "rush_yards", "rush_tds", "tgt", "receptions", "receiving_yards",
          "receiving_tds", "ret_yds", "ret_td", "two_pt", "fumbles_lost"]


def valid_row(tokens, lead=(), strict=True):
    """Structural check: 20 tokens, %Ros ends with %, every non-dash numeric token is a
    plain number, every per-category stat has a decimal point (except pass_yards and
    the integer-rendered rec yards / ranks / GP / bye)."""
    if len(tokens) != 20:
        return False
    # leading roster-status text (fantasy team name / FA) must not contain numeric-looking tokens:
    # a stray number there means the 20-token slice is shifted by a split value.
    if any(NUM.match(t) or t.endswith("%") for t in lead):
        return False
    d = dict(zip(FIELDS, tokens))
    if not d["ros"].endswith("%") or not NUM.match(d["ros"][:-1]):
        return False
    for k in FIELDS:
        if k == "ros":
            continue
        v = d[k]
        if v == "-":
            continue
        if not NUM.match(v):
            return False
    for k in ("fan", "pass_tds", "interceptions", "rush_attempts", "rush_yards", "rush_tds",
              "tgt", "receptions", "receiving_tds", "two_pt", "fumbles_lost"):
        v = d[k]
        if strict and v != "-" and "." not in v:
            return False
    if d["fan"] == "-":
        return False
    for k in ("gp", "bye", "pre", "act"):
        if d[k] != "-" and "." in d[k]:
            return False
    return True


def blocks(text):
    """Yield (name_line, header_match, tokens, page_idx) for every player block."""
    pages = text.split("\x0c")
    for pi, page in enumerate(pages):
        lines = page.split("\n")
        for i, ln in enumerate(lines):
            m = HDR.match(ln)
            if not m or i == 0:
                continue
            name = lines[i - 1].strip()
            if not name or HDR.match(lines[i - 1]):
                continue
            toks = re.sub(r"[\ue000-\uf8ff]", " ", m.group(3)).split()
            yield name, m, toks[-20:], toks, pi, i


def clean(name):
    parts = name.split()
    while len(parts) > 1 and parts[-1] in STATUS_TAGS:
        parts.pop()
    return " ".join(parts)


def num(v):
    return None if v == "-" else float(v.replace(",", ""))


def main():
    players, glitched, recovered, dropped = {}, [], [], []
    global RELAXED_ALL
    RELAXED_ALL = []
    seen_order = []
    for f in sorted(glob.glob(os.path.join(TXT_DIR, "*.txt"))):
        text = open(f, encoding="utf-8").read()
        rows = list(blocks(text))
        by_page = {}
        for r in rows:
            by_page.setdefault(r[4], []).append(r[5])
        def is_strict(r):
            return r[5] in sorted(by_page[r[4]])[-2:]
        relaxed = []
        # clean rows keyed by (name, team, pos) from this file (first valid occurrence wins)
        clean_rows = {}
        for name, m, last20, alltoks, pi, i in rows:
            if valid_row(last20, alltoks[:-20], strict=is_strict((name, m, last20, alltoks, pi, i))):
                clean_rows.setdefault((name, m.group(1), m.group(2)), last20)
        done_in_file = set()
        for name, m, last20, alltoks, pi, i in rows:
            key = (name, m.group(1), m.group(2))
            if key in done_in_file:
                continue
            if valid_row(last20, alltoks[:-20], strict=is_strict((name, m, last20, alltoks, pi, i))):
                toks = last20
                if not valid_row(last20, alltoks[:-20], strict=True):
                    relaxed.append((os.path.basename(f), name))
            else:
                glitched.append((os.path.basename(f), name))
                # clean duplicate re-printed by Yahoo elsewhere in the same file
                alt = [r for r in rows if (r[0], r[1].group(1), r[1].group(2)) == key and valid_row(r[2], r[3][:-20], strict=is_strict(r))]
                if alt:
                    toks = alt[0][2]
                    recovered.append((os.path.basename(f), name))
                else:
                    dropped.append((os.path.basename(f), name, last20))
                    done_in_file.add(key)
                    continue
            done_in_file.add(key)
            RELAXED_ALL.extend(x for x in relaxed if x[1] == name)
            d = dict(zip(FIELDS, toks))
            rec = {
                "clean_name": clean(name),
                "raw_name": name,
                "team_abbr": m.group(1),
                "position": m.group(2),
                "pre_season_rank": num(d["pre"]),
                "actual_rank": num(d["act"]),
                "pass_yards": num(d["pass_yards"]),
                "pass_tds": num(d["pass_tds"]),
                "interceptions": num(d["interceptions"]),
                "rush_attempts": num(d["rush_attempts"]),
                "rush_yards": num(d["rush_yards"]),
                "rush_tds": num(d["rush_tds"]),
                "receptions": num(d["receptions"]),
                "receiving_yards": num(d["receiving_yards"]),
                "receiving_tds": num(d["receiving_tds"]),
                "fumbles_lost": num(d["fumbles_lost"]),
                "_fan": num(d["fan"]),
            }
            players[(clean(name), m.group(1), m.group(2))] = rec
            seen_order.append((clean(name), m.group(1), m.group(2)))
    out = [players[k] for k in dict.fromkeys(seen_order)]
    out.sort(key=lambda r: -(r["_fan"] or 0))
    json.dump(out, open(OUT, "w"), indent=1)
    print(f"players parsed: {len(out)}")
    print(f"glitched rows detected: {len(glitched)}  recovered from clean duplicate: {len(recovered)}  dropped: {len(dropped)}")
    for g in glitched:
        print("  glitched:", g)
    for d in dropped:
        print("  DROPPED:", d)
    print(f"mid-page rows with whole-number renderings accepted (no decimal point): {len(RELAXED_ALL)}")
    for x in RELAXED_ALL:
        print("  ", x)
    tagged = [r for r in out if r["raw_name"] != r["clean_name"]]
    print(f"names with status tags stripped: {len(tagged)}")
    for r in tagged:
        print("  ", repr(r["raw_name"]), "->", r["clean_name"])


if __name__ == "__main__":
    main()
