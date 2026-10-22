# Industrial Site Map

An interactive map that scores every U.S. county as a location for a **machine shop**, **manufacturing plant**, **warehouse / distribution center** or **logistics hub**.

**Live site:** https://kes30033.github.io/Industrial-Site-Map/

The map colors counties by rank (darkest blue = top 1% of counties). Every factor has a weight slider, starting with all factors weighted equally, so you can tune the score to your own priorities; under each slider is which counties that factor favors and a link to its data source. Must-have filters (state, distance to interstate / Class I rail / port, right-to-work, metro only) take counties out of the ranking entirely. Click any county for the evidence behind its score: nearest rail yard, intermodal terminal, port and cargo airport; workforce counts; welder, machinist and electrician concentration and pay in the local labor market; and state tax, regulation and power-price figures. Settings are saved in the URL, so you can share a link to a specific view, and the ranked list downloads as CSV.

## What goes into the score

| Group | Factors | Source |
|---|---|---|
| Freight access | Distance to Interstate, Class I rail line, rail yard, intermodal (container) rail terminal, nearest dock of a port handling 5M+ tons/yr, air cargo airport; population within 250 miles | BTS National Transportation Atlas Database, USACE principal ports and docks (Navigation Facilities), FAA all-cargo landed weight, Census population estimates |
| Workforce | Production & skilled-trade workers within 45 miles; drivers & material movers within 45 miles; metalworking and warehousing industry jobs within 45 miles; welder/machinist/CNC and electrician/mechanic concentration; trade and driver pay | Census ACS 5-year (C24010), BLS QCEW, BLS OEWS (metro and nonmetro areas) |
| Taxes, regulation & costs | Tax Foundation overall, corporate and property tax ranks; CNBC business-friendliness rank; count of state regulatory restrictions; right-to-work; industrial electricity price | Tax Foundation, CNBC Top States for Business, Mercatus QuantGov State RegData, NCSL, EIA |

Each factor becomes a 0–100 score (distance factors halve every N miles; counts use percentile rank; state ranks map 1→100, 50→0), and the overall score is the weighted average. The site's "How scores work" panel lists every factor, its formula and the exact source vintage.

**Facility presets** (machine shop, plant, warehouse, logistics hub) are still defined in `site/config.js` but the picker is hidden for now. Old shared links with `f=<preset>` still load that preset's weights.

## How it's built

```
pipeline/fetch.py    downloads every source into cache/ (saved to the data-cache branch)
pipeline/build.py    computes per-county distances, labor sheds and state tables -> site/data/
site/                the static website (Leaflet map, no build step)
site/config.js       factors, scoring rules, source links and default weights: edit this to change weights
.github/workflows/build.yml   runs fetch + build monthly and deploys site/ to the gh-pages branch
scripts/qa.mjs       headless-browser check run by the workflow; screenshots land on the qa-results branch
```

The workflow runs on the 4th of every month, on any push that changes `site/` or `pipeline/`, and on demand from the **Actions** tab (**Build data and deploy site → Run workflow**). If a source is down, the previous cached copy is used, so the site keeps building.

Each July, CNBC publishes a new Top States for Business ranking at a new URL. Add it to the top of `CNBC_URLS` in `pipeline/fetch.py`. Everything else finds the newest release automatically.

### Run locally

```bash
pip install -r pipeline/requirements.txt
npm install -g mapshaper
python pipeline/fetch.py      # or: git archive origin/data-cache | tar -x -C cache
python pipeline/build.py
python -m http.server -d site 8000
```

## Limits

Distances are straight-line from each county's population center, not drive times, and counties can be large, so treat this as a shortlist tool. Land prices, utility capacity, zoning, incentives and permitting aren't included. Tax and regulation figures are state-level.
