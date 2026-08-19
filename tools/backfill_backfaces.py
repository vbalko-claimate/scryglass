#!/usr/bin/env python3
"""Add DFC BACK-FACE rows to data/cards_cache.json (task #15).

A transformed permanent's GameObject carries the BACK face's grpId; the cache
held only front faces, so the recon named every transformed DFC
"Unknown(<grp>)" — the advisor could not see a transformed threat by name.
Found via the belief two-sided corpus (21 dropped public cards).

The MTGA CardDatabase links faces explicitly (Cards.LinkedFaceGrpIds), so no
+1 heuristic is involved. Names come from Localizations_enUS the same way the
legacy importer reads them. The MTGA DB is opened READ-ONLY IMMUTABLE.

Usage: python3 tools/backfill_backfaces.py [Raw_CardDatabase path]
"""
import glob
import json
import sqlite3
import sys
from pathlib import Path

CACHE = Path(__file__).parent.parent / "data" / "cards_cache.json"


def main():
    if len(sys.argv) > 1:
        db = sys.argv[1]
    else:
        pat = (Path.home() / "Library/Application Support/Steam/steamapps/"
               "common/MTGA/MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga")
        hits = sorted(glob.glob(str(pat)))
        if not hits:
            sys.exit("no MTGA CardDatabase found; pass a path")
        db = hits[-1]
    cache = json.loads(CACHE.read_text())
    have = {c["grp_id"] for c in cache if isinstance(c, dict) and "grp_id" in c}
    c = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)

    linked = set()
    for (lf,) in c.execute(
            "SELECT LinkedFaceGrpIds FROM Cards WHERE LinkedFaceGrpIds IS NOT "
            "NULL AND LinkedFaceGrpIds != ''"):
        for tok in str(lf).replace(";", ",").split(","):
            if tok.strip().isdigit():
                linked.add(int(tok.strip()))

    added = []
    for g in sorted(linked - have):
        row = c.execute(
            "SELECT c.GrpId, l.Loc FROM Cards c JOIN Localizations_enUS l "
            "ON c.TitleId = l.LocId AND l.Formatted = 1 WHERE c.GrpId=?",
            (g,)).fetchone()
        if not row or not row[1]:
            continue
        added.append({
            "grp_id": g,
            "name": row[1],
            # Minimal row: the recon needs the NAME. Richer fields stay empty
            # rather than guessed — an invented type line would be worse than
            # an absent one.
            "card_types": [],
            "subtypes": [],
            "colors": [],
            "abilities": [],
            "oracle_text": "",
        })
    if not added:
        print("nothing to add")
        return
    cache.extend(added)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False))
    print(f"added {len(added)} linked-face rows")
    probe = {a["grp_id"]: a["name"] for a in added}
    for g in (78896, 79583, 78938):
        print(f"  {g} -> {probe.get(g)}")


if __name__ == "__main__":
    main()
