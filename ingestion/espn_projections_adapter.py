"""
Phase 4C real adapter #2: ESPN's "Complete 2026 Projections" page, saved as
PDF (File > Print > Save as PDF from fantasy.espn.com/football/players/projections).

Same acquisition-only boundary as AthleticSeasonXlsxAdapter: this file reads
the saved PDF and produces NormalizedObservations. No trust or persistence
logic lives here — every observation still goes through the unmodified
shared pipeline.

Scope, decided deliberately rather than defaulted:
- Only "2026 PROJECTIONS" is imported (data_type='projection', scope='season').
  ESPN's "2025 STATISTICS" column is last season's historical box score —
  useful as human context, but not what "compare 2026 projections vs 2026
  actuals" needs. Real 2026 actuals come from the NFL canonical source at
  weekly granularity, already correctly modeled. Importing ESPN's 2025 column
  would require extending the `statistics` table with the same scope/nullable-
  week_id machinery projections/rankings already got, for a table this app
  doesn't need populated from this source. Deliberately not done.
- D/ST and K rows are skipped entirely — this app only tracks QB/RB/WR/TE
  (an early Phase 1 scope decision), and D/ST and K have entirely different
  stat schemas anyway.
- No ranking data on this page at all (it's a projections table, not a
  ranking list) — this adapter produces projections only.

Known, real limitation of parsing a *printed* PDF export (not a clean data
file): ESPN's page layout gets cut off at the print margin, which
consistently drops the LAST stat column for every position — QB rushing TDs,
RB receiving TDs, WR/TE rushing TDs are simply never present in this specific
export format. This isn't guessed around or backfilled; those observations
are just never produced, which is exactly how "missing" is supposed to work
here — no different in kind from a value ESPN itself never published.
"""
import re
import subprocess
from typing import List

from ingestion.collector import SourceAdapter, NormalizedObservation, AdapterUnavailable

_SPLIT_COL = 56  # historical constant, no longer used for splitting (see 2026-09-18
                 # fix below) -- kept only because nothing else in this file referenced
                 # it and removing it isn't necessary to fix the actual bug.
                 #
                 # 2026-09-18 fix: this WAS a fixed column offset the original author
                 # "empirically confirmed" against one real PDF export. Against a real,
                 # larger 14-page "Complete 2026 Projections" export (the genuine Full
                 # Projections tab, not Sortable), it silently broke: the right-hand
                 # "YEAR / 2025 STATISTICS / ..." column's actual start position varies
                 # per player block (observed at column 61 on page 1, column 31-33 on
                 # later pages) -- pdftotext's -layout output isn't a truly fixed-width
                 # grid across an entire multi-page print job; it shifts with whatever
                 # left-column content (name length, ad-banner overlap) is on that line.
                 # A fixed split point only worked for whichever blocks happened to sit
                 # right of it; every block whose YEAR column started BEFORE column 56
                 # was silently dropped (no error -- the block-boundary regex just never
                 # matched, so those players never became a block at all). Concretely,
                 # this dropped all but 3 of the players in a real 14-page, ~400-player
                 # export the very first time it was run against genuine data. Replaced
                 # with a per-line regex search for "YEAR" as a whole word, wherever it
                 # actually falls on that line -- no fixed offset assumption.

_POSITIONS = {"QB", "RB", "WR", "TE"}
_STATUS_WORDS = {"Questionable", "Injured Reserve", "Out", "Suspended", "Doubtful"}

# Position-aware column index -> our stat_name. Index positions come from
# ESPN's own header row (e.g. QB: C/A, YDS, TD, INT, CAR, YDS, [TD, truncated]).
# None means "ESPN reports this but we don't track it" (CAR/attempts counts,
# AVG, targets) — skipped, not guessed into something we do track.
_QB_COLUMNS = [None, "pass_yards", "pass_tds", "interceptions", None, "rush_yards", "rush_tds"]
_RB_COLUMNS = [None, "rush_yards", None, "rush_tds", "receptions", "receiving_yards", "receiving_tds"]
_WR_TE_COLUMNS = [None, "receptions", "receiving_yards", None, "receiving_tds", None, "rush_yards", "rush_tds"]

_PROJECTION_CONFIDENCE = 0.85  # right at the auto-accept threshold: a real value parsed
                                # from PDF-derived text is less certain than a clean
                                # spreadsheet cell (Athletic adapter uses 0.9) — text-
                                # position parsing has more ways to be subtly wrong.


class ESPNSeasonProjectionsAdapter(SourceAdapter):
    """file_path may be a single PDF or a directory of PDFs (ESPN's listing is
    paginated across up to 21 saved pages; passing a directory processes all
    of them in one fetch() call)."""

    def __init__(self, source_name: str, file_path: str):
        self.source_name = source_name
        self.file_path = file_path

    def fetch(self, season_id: int, week_number: int) -> List[NormalizedObservation]:
        import os
        if os.path.isdir(self.file_path):
            pdf_paths = sorted(
                os.path.join(self.file_path, f) for f in os.listdir(self.file_path) if f.lower().endswith(".pdf")
            )
            if not pdf_paths:
                raise AdapterUnavailable(f"No PDF files found in directory: {self.file_path}", transient=True)
        elif os.path.isfile(self.file_path):
            pdf_paths = [self.file_path]
        else:
            raise AdapterUnavailable(f"Source path not found: {self.file_path}", transient=True)

        observations: List[NormalizedObservation] = []
        for path in pdf_paths:
            text = self._extract_text(path)
            observations.extend(self._parse_text(text))
        return observations

    def _extract_text(self, pdf_path: str) -> str:
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", pdf_path, "-"],
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError as e:
            raise AdapterUnavailable("pdftotext is not installed (part of poppler-utils)", transient=False) from e
        except subprocess.TimeoutExpired as e:
            raise AdapterUnavailable(f"pdftotext timed out on {pdf_path}", transient=True) from e
        if result.returncode != 0:
            raise AdapterUnavailable(f"pdftotext failed on {pdf_path}: {result.stderr}", transient=False)
        return result.stdout

    _YEAR_RE = re.compile(r"\bYEAR\b")
    # Ordered markers for the right-hand ("YEAR"/stats) column. Checked in this
    # order per line since "2025 STATISTICS"/"2026 PROJECTIONS"/"2026 OUTLOOK:"
    # can share a physical line with the left-hand (name/team-pos/status)
    # column -- the two columns are printed side by side, and how much
    # vertical padding separates them varies per player block (observed both
    # "name" and "name    2025 STATISTICS  <numbers>" on one line in the same
    # real export), so every line needs this same left/right split, not just
    # the header line.
    _RIGHT_MARKERS = ("2025 STATISTICS", "2026 PROJECTIONS", "2026 OUTLOOK:", "YEAR")
    _NAV_NOISE = ("NFL", "NBA", "MLB", "NCAAF", "Soccer", "Tennis", "NHL", "More Sports",
                  "Watch", "Where to Watch", "Reset All", "Player Name",
                  "Fantasy Football", "Sign Up")

    def _split_line(self, line: str):
        """Dynamic replacement for the old fixed-column split: find whichever
        right-column marker appears first on this line and split there. No
        marker on the line -> the whole (stripped) line is the left side."""
        best = None
        for marker in self._RIGHT_MARKERS:
            idx = line.find(marker)
            if idx != -1 and (best is None or idx < best):
                best = idx
        if best is None:
            return line.strip(), ""
        return line[:best].strip(), line[best:].strip()

    def _parse_text(self, text: str) -> List[NormalizedObservation]:
        lines = text.split("\n")

        # Group into blocks, each anchored by a "YEAR ..." right-column header
        # (found via marker search, not a fixed column offset -- see _SPLIT_COL
        # comment above for why a fixed offset silently dropped most blocks).
        blocks = []
        current_header, current = None, []
        for line in lines:
            left, right = self._split_line(line)
            if right.startswith("YEAR"):
                if current_header is not None:
                    blocks.append((current_header, current))
                current_header, current = right, []
                if left:
                    current.append((left, ""))  # e.g. "A.J. Brown" sharing the header's own line
            elif current_header is not None:
                current.append((left, right))
        if current_header is not None:
            blocks.append((current_header, current))

        observations = []
        for header, block in blocks:
            observations.extend(self._parse_block(header, block))
        return observations

    def _parse_block(self, header: str, block) -> List[NormalizedObservation]:
        header_tokens = header.split()[1:]  # drop leading "YEAR"

        name, team_pos, proj_tokens = None, None, None
        for left, right in block:
            if right.startswith("2026 PROJECTIONS"):
                proj_tokens = right[len("2026 PROJECTIONS"):].split()
            if not left or left in _STATUS_WORDS or left == "PLAYER" or any(
                    noise in left for noise in self._NAV_NOISE):
                continue
            if team_pos is None and self._ends_with_position(left):
                team_pos = left
            elif name is None and not self._ends_with_position(left):
                name = left

        if not name or not team_pos or proj_tokens is None:
            return []  # couldn't confidently locate all three — skip, don't guess

        position = team_pos.split()[-1]
        if position not in _POSITIONS:
            return []  # D/ST, K, or unrecognized shape — out of scope for this app

        column_map = (_QB_COLUMNS if position == "QB" else
                      _RB_COLUMNS if position == "RB" else
                      _WR_TE_COLUMNS)  # WR and TE share a column shape

        observations = []
        for idx, (header_tok, value_tok) in enumerate(zip(header_tokens, proj_tokens)):
            if header_tok == "C/A":
                observations.extend(self._parse_completions_attempts(name, position, value_tok))
                continue
            if idx >= len(column_map) or column_map[idx] is None:
                continue
            if value_tok == "--":
                continue  # missing, not zero — no observation emitted at all
            try:
                value = float(value_tok)
            except ValueError:
                continue  # unparseable token — skip rather than guess
            observations.append(NormalizedObservation(
                player_name_raw=name, position_hint=position, data_type="projection",
                stat_name=column_map[idx], value=value, confidence=_PROJECTION_CONFIDENCE, scope="season",
            ))
        return observations

    def _parse_completions_attempts(self, name: str, position: str, token: str) -> List[NormalizedObservation]:
        if "/" not in token:
            return []
        comp, att = token.split("/", 1)
        observations = []
        for stat_name, raw in (("pass_completions", comp), ("pass_attempts", att)):
            if raw == "--":
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            observations.append(NormalizedObservation(
                player_name_raw=name, position_hint=position, data_type="projection",
                stat_name=stat_name, value=value, confidence=_PROJECTION_CONFIDENCE, scope="season",
            ))
        return observations

    @staticmethod
    def _ends_with_position(text: str) -> bool:
        tokens = text.split()
        return bool(tokens) and tokens[-1] in _POSITIONS
