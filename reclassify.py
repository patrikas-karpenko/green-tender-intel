"""
Reclassify lots for precision — the trust fix.

We were labelling sub-sectors from the raw CPV code, which is noisy: broad
construction/infrastructure jobs that merely include a green CPV got tagged
"Solar PV" etc. But TED's *notice title* is English and embeds the official CPV
category ("... Water-treatment plant construction work ...", "... Energy
Performance Contract ..."). So we classify off that English text instead, and
tag each lot with a relevance:

    core     — the title names a real green technology (solar, heat pump, ...)
    adjacent — a green CPV, but a generic title (keep, lower confidence)
    noise    — a green CPV on a clearly non-green job (school/road/irrigation)

Reports then hide `noise`, and real items get the correct sub-sector.

Adds a `relevance` column to lots. Dry-run by default — shows what WOULD change.

Usage:
    python reclassify.py --niche green            # dry-run: report only
    python reclassify.py --niche green --apply     # write the changes
"""
from dotenv import load_dotenv
load_dotenv()

import argparse, re, collections
import core
from psycopg2.extras import RealDictCursor
from tqdm import tqdm

# --- green technologies, checked in order; first hit wins (English-first, with
#     a few common PL/FR/DE/IT/ES stems for the original-language descriptions) ---
GREEN = [
    ("Solar PV", r"photovolta|fotowolta|\bpv\b|solar (panel|module|park|farm|plant|power|array|installation|system)|agrivolta"),
    ("Heat pumps", r"heat pump|pompe? à? chaleur|w[äa]rmepumpe|pompa ciep[łl]a|pompa di calore|bomba de calor"),
    ("Wind", r"wind (turbine|farm|power|energy|park)|offshore wind|[ée]olien|windkraft|elektrownia wiatrowa"),
    ("Solar (other)", r"solar (thermal|collector|heating|hot water)|thermal solar|kolektor s[łl]oneczn|solaire thermique|\bsolar\b|solaire"),
    ("Energy efficiency", r"energy (efficiency|performance|renovation|retrofit|saving)|energy performance contract|"
                          r"thermal (insulation|renovation)|building energy|termomoderniz|"
                          # FR / DE / IT / ES thermal-renovation & energy-performance
                          r"r[ée]habilitation (thermique|[ée]nerg[ée]tique)|requalification thermique|"
                          r"r[ée]novation (thermique|[ée]nerg[ée]tique)|isolation thermique|"
                          r"performance [ée]nerg[ée]tique|efficacit[ée] [ée]nerg[ée]tique|"
                          r"energetische sanierung|efficienza energetica|riqualificazione energetica|"
                          r"rehabilitaci[óo]n energ[ée]tica|eficiencia energ[ée]tica"),
    ("Power plant / grid", r"cogeneration|combined heat and power|\bchp\b|district heating|biomass (heat|plant|boiler)|"
                           r"hydro-?electric|micro-?hydro|substation|grid (connection|upgrade|reinforcement)"),
]
GREEN = [(lbl, re.compile(p, re.I)) for lbl, p in GREEN]

# clearly non-green construction / infrastructure / supply jobs
NOISE = re.compile(
    r"school building|construction work for school|hospital (construction|building)|"
    r"\broad\b|bridge|masonry|reservoir construction|irrigation|water[- ]treatment|"
    r"sewage|structural shell|building construction work|retirement home|nursing home|"
    r"gymnasium|sports (hall|complex|facilit|cent(re|er))|swimming pool|"
    r"\bfurniture\b|kindergarten construction|office building|"
    r"bus (garage|depot|shelter)|car park|demolition|"
    r"restructuring work|extension work|refurbishment of the building", re.I)

# CPV-prefix fallback (same rules the ingester uses)
CPV_RULES = [("09331", "Solar PV"), ("09332", "Solar PV"), ("45261215", "Solar PV"),
             ("09330", "Solar (other)"), ("31121340", "Wind"), ("42511", "Heat pumps"),
             ("45251100", "Power plant / grid"), ("71314", "Energy efficiency")]


def cpv_subsector(cpv):
    for prefix, label in CPV_RULES:
        if cpv and cpv.startswith(prefix):
            return label
    return "Other green"


def classify(title, desc, cpv):
    """Return (subsector, relevance). The title is the trustworthy signal (it's
    English and carries the CPV label); the description is a weaker fallback.
      1. green tech named in the TITLE            -> core
      2. green tech only in the DESCRIPTION       -> adjacent (rescued, low conf)
      3. no green anywhere + non-green TITLE       -> noise (hidden)
      4. otherwise                                 -> adjacent via CPV
    Order matters: description-rescue runs BEFORE noise, so anything genuinely
    green is never hidden."""
    for label, pat in GREEN:
        if pat.search(title):
            return label, "core"
    for label, pat in GREEN:
        if desc and pat.search(desc):
            return label, "adjacent"
    if NOISE.search(title):
        return "Other", "noise"
    return cpv_subsector(cpv), "adjacent"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--niche", default="green")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--samples", type=int, default=12, help="example rows to print per bucket")
    args = ap.parse_args()

    conn = core.get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    if args.apply:
        cur.execute("ALTER TABLE lots ADD COLUMN IF NOT EXISTS relevance text")
        conn.commit()

    cur.execute("""SELECT l.id, l.subsector AS old_sub, l.cpv_main,
                          t.title AS notice_title, l.title AS lot_title, l.title_en,
                          COALESCE(l.description_en, l.description_original) AS descr
                   FROM lots l JOIN tenders t ON t.publication_number=l.publication_number
                   WHERE t.niche=%s""", (args.niche,))
    rows = cur.fetchall()

    trans = collections.Counter()      # (old_sub -> new_sub) counts
    rel_counts = collections.Counter()
    noise_samples, changed_samples = [], []

    seen_noise, seen_changed = set(), set()   # dedup samples by title (corrigenda repeat)
    updates = []
    for r in tqdm(rows, desc="Classifying"):
        title = " ".join(x for x in (r["notice_title"], r["title_en"], r["lot_title"]) if x)
        desc = r["descr"] or ""
        new_sub, rel = classify(title, desc, r["cpv_main"])
        trans[(r["old_sub"], new_sub)] += 1
        rel_counts[rel] += 1
        nt = (r["notice_title"] or "")[:70]
        if rel == "noise" and nt not in seen_noise and len(noise_samples) < args.samples:
            seen_noise.add(nt); noise_samples.append((r["old_sub"], r["notice_title"]))
        if new_sub != r["old_sub"] and rel != "noise" and nt not in seen_changed and len(changed_samples) < args.samples:
            seen_changed.add(nt); changed_samples.append((r["old_sub"], new_sub, rel, r["notice_title"]))
        updates.append((new_sub, rel, r["id"]))

    total = len(rows)
    print(f"\n{total} lots in niche '{args.niche}'\n")
    print("Relevance breakdown:")
    for rel in ("core", "adjacent", "noise"):
        n = rel_counts[rel]
        print(f"  {rel:9} {n:6}  ({n/total*100:4.1f}%)")

    print("\nBiggest sub-sector changes (old → new: count):")
    for (old, new), n in trans.most_common(15):
        if old != new:
            print(f"  {str(old):22} → {str(new):20} {n}")

    print(f"\nSample NOISE (green CPV, non-green title) — these get hidden:")
    for old, title in noise_samples:
        print(f"  [{old}] {(title or '')[:78]}")

    print(f"\nSample RELABELLED (kept, better sub-sector):")
    for old, new, rel, title in changed_samples:
        print(f"  {old} → {new} [{rel}]: {(title or '')[:60]}")

    if args.apply:
        # group by (subsector, relevance) and update each group in ONE statement
        # via WHERE id = ANY(...) — ~20 statements instead of one per row.
        groups = collections.defaultdict(list)
        for new_sub, rel, lid in updates:
            groups[(new_sub, rel)].append(lid)
        for (new_sub, rel), ids in tqdm(groups.items(), desc="Applying", total=len(groups)):
            cur.execute("UPDATE lots SET subsector=%s, relevance=%s WHERE id = ANY(%s)",
                        (new_sub, rel, ids))
        conn.commit()
        print(f"\nApplied: updated {len(updates)} lots in {len(groups)} batches.")
    else:
        print(f"\n(dry-run — nothing written. Re-run with --apply to commit.)")

    cur.close(); conn.close()


if __name__ == "__main__":
    main()