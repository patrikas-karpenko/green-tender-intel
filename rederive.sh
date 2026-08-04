#!/usr/bin/env bash
# One ordered, idempotent re-derive of every DERIVED layer.
#   awards -> companies (enrichment preserved) -> value flags
#          -> contract ends -> lot relevance -> corporate groups
# Nothing else should touch the database while this runs.
set -euo pipefail
NICHE="${1:-green}"

echo "== 1/8  extract award winners ================================"
python extract_awards_xml.py --niche "$NICHE"
echo "== 2/8  normalize companies (enrichment preserved) ==========="
python normalize.py
echo "== 3/8  normalize buyers ====================================="
python normalize_buyers.py
echo "== 4/8  enrich companies (GLEIF — new/unenriched only) ======="
python enrich_companies.py
echo "== 5/8  flag fat-finger values ==============================="
python flag_values.py --apply
echo "== 6/8  fill contract end dates =============================="
python add_contract_end.py --niche "$NICHE"
echo "== 7/8  reclassify lots (relevance + sub-sectors) ============"
python reclassify.py --niche "$NICHE" --apply
echo "== 8/8  rebuild corporate groups ============================="
python build_groups.py --apply
echo ""
echo "Re-derive complete — every derived layer is consistent."