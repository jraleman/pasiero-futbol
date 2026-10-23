#!/usr/bin/env python3
"""
Fictional names for every club's shirt numbers, for the synthetic season.

The device and the contract know players by shirt number only. The demo's
screens, sentences and the assistant put a name next to the number, so a
fan or an analyst can follow a player through the match. The names are made
up: seeded per club code, so the same club gets the same squad every run,
and no real player is named. A match whose clubs aren't in league.json (the
real tracked match, the scouting footage) gets no names.

    python3 iot/scripts/squads.py            # writes iot/ingest/analysis/squads.json
    python3 iot/scripts/squads.py --check    # fails if the file isn't what it would write

Standard library only.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEAGUE = ROOT / "iot" / "ingest" / "analysis" / "league.json"
OUT = ROOT / "iot" / "ingest" / "analysis" / "squads.json"
NUMBERS = range(1, 31)

FIRST = ("Adam", "Ade", "Alfie", "Amir", "Andre", "Ben", "Bruno", "Caleb", "Callum", "Dario", "Declan", "Eli",
         "Emil", "Ethan", "Felix", "Finn", "Gabriel", "Hamza", "Harvey", "Hugo", "Ibrahim", "Isaac", "Jai", "Jonas",
         "Jude", "Kai", "Kofi", "Leo", "Luca", "Luis", "Malik", "Marco", "Mateo", "Max", "Milan", "Nathan", "Niko",
         "Noah", "Oscar", "Owen", "Pablo", "Rafael", "Reece", "Rio", "Ruben", "Sami", "Sean", "Theo", "Tobias",
         "Tomas", "Victor", "Wes", "Yusuf", "Zane")
LAST = ("Abbott", "Adeyemi", "Almeida", "Ashby", "Bakker", "Barlow", "Bianchi", "Brandt", "Calloway", "Carvalho",
        "Corrigan", "Dalgaard", "Delacroix", "Dunmore", "Eastwood", "Ekwueme", "Fairley", "Ferreira", "Galloway",
        "Hartley", "Haugen", "Holloway", "Ingram", "Iwobi-Lane", "Jansen", "Kane-Moss", "Kowalczyk", "Lindqvist",
        "Lowther", "Maddox", "Marchetti", "Mensah", "Moreau", "Nakamura", "Novak", "Oduya", "Okafor", "Orsini",
        "Pemberton", "Petrov", "Quaye", "Radley", "Rinaldi", "Rourke", "Sandoval", "Selby", "Sørensen", "Tavares",
        "Thorne", "Ugarte-Bell", "Varga", "Voss", "Wakefield", "Whitlock", "Yilmaz", "Zielinski")


def squad(code: str) -> dict[str, str]:
    """Numbers 1-30 to fictional names, the same every run for a club code, no surname twice."""
    rng = random.Random(f"squad:{code}")
    surnames = rng.sample(LAST, len(NUMBERS))
    return {str(n): f"{rng.choice(FIRST)} {surname}" for n, surname in zip(NUMBERS, surnames)}


def build() -> dict:
    league = json.loads(LEAGUE.read_text(encoding="utf-8"))
    return {
        "_about": "Fictional names for the synthetic season's shirt numbers (iot/scripts/squads.py), seeded per "
                  "club code. No real player is named; the contract carries shirt numbers only.",
        "synthetic": True,
        # The real match's teams keep their numbers: its players are real people
        "clubs": {code: squad(code) for code, c in sorted(league["clubs"].items()) if not c.get("dataset")},
    }


def main() -> int:
    text = json.dumps(build(), ensure_ascii=False, indent=1) + "\n"
    if "--check" in sys.argv[1:]:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run iot/scripts/squads.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(build()['clubs'])} clubs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
