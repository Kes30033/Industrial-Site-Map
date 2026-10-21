/* Scoring configuration: factors, how each is turned into a 0-100 score, and the facility presets.
 *
 * Score types
 *   dist     100 at 0 miles, halving every `half` miles  (100 * 0.5^(miles / half))
 *   pct      percentile rank among all U.S. counties (higher raw value = better)
 *   pct_inv  percentile rank, reversed (lower raw value = better)
 *   rank     state rank 1-50 (1 = best) mapped to 100..0
 *   state_inv percentile among states, reversed (lower raw value = better)
 *   bool     100 if true, 0 if false
 * Missing values score a neutral 50.
 */
window.ISM_CONFIG = {
  groups: [
    { id: 'freight', label: 'Freight access' },
    { id: 'labor', label: 'Workforce' },
    { id: 'business', label: 'Taxes, regulation & costs' },
  ],

  factors: [
    // ---- Freight access
    { id: 'interstate', group: 'freight', label: 'Interstate highway', src: 'd_interstate', type: 'dist', half: 10,
      about: 'Straight-line miles from the county’s population center to the nearest Interstate (FHWA Eisenhower Interstate System).' },
    { id: 'class1', group: 'freight', label: 'Class I rail line', src: 'd_class1', type: 'dist', half: 8,
      about: 'Miles to the nearest track owned by a Class I railroad (BNSF, UP, CSX, NS, CN, CPKC). Plants that need a rail spur want this under ~5 miles.' },
    { id: 'yard', group: 'freight', label: 'Rail yard', src: 'd_yard', type: 'dist', half: 15,
      about: 'Miles to the nearest rail yard of any railroad, including short lines (BTS rail yards layer).' },
    { id: 'intermodal', group: 'freight', label: 'Intermodal rail terminal', src: 'd_intermodal', type: 'dist', half: 40,
      about: 'Miles to the nearest container / trailer-on-flatcar rail terminal, where truck freight transfers to rail. Drayage beyond ~100 miles gets expensive.' },
    { id: 'port', group: 'freight', label: 'Seaport or river port', src: 'd_port', type: 'dist', half: 75,
      about: 'Miles to the boundary of the nearest principal port handling 5 million+ short tons a year (USACE). Includes Great Lakes and inland river ports.' },
    { id: 'air', group: 'freight', label: 'Air cargo airport', src: 'd_air', type: 'dist', half: 50,
      about: 'Miles to the nearest airport with 250 million+ lbs of all-cargo landed weight (FAA).' },
    { id: 'market', group: 'freight', label: 'People within 250 miles', src: 'market_pop', type: 'pct',
      about: 'Population within 250 miles, roughly a same-day truck run. A proxy for how many customers a distribution site can reach.' },

    // ---- Workforce
    { id: 'trades', group: 'labor', label: 'Production & skilled-trade workers', src: 'shed_trades', type: 'pct',
      about: 'Residents working in production, construction & extraction, or installation, maintenance & repair occupations within 45 miles (ACS 5-year). Includes machinists, welders, electricians and mechanics.' },
    { id: 'logi', group: 'labor', label: 'Drivers & material movers', src: 'shed_logi', type: 'pct',
      about: 'Residents working in transportation and material-moving occupations within 45 miles (ACS 5-year).' },
    { id: 'metal', group: 'labor', label: 'Metalworking & machinery industry', src: 'shed_metal', type: 'pct',
      about: 'Private jobs in fabricated metal, machinery and transportation-equipment manufacturing within 45 miles (BLS QCEW). Both an experienced labor pool and a customer base for job shops.' },
    { id: 'whse', group: 'labor', label: 'Warehousing & trucking industry', src: 'shed_whse', type: 'pct',
      about: 'Private jobs in warehousing & storage and truck transportation within 45 miles (BLS QCEW).' },
    { id: 'lq_metal', group: 'labor', label: 'Welder & machinist concentration', src: 'lq_metal', type: 'pct',
      about: 'How concentrated welders, machinists and CNC operators are in the local labor market compared with the U.S. (average OEWS location quotient; 1.0 = national share). Uses the metro area, or the state’s nonmetro areas for rural counties.' },
    { id: 'lq_elec', group: 'labor', label: 'Electrician & mechanic concentration', src: 'lq_elec', type: 'pct',
      about: 'Concentration of electricians and industrial machinery mechanics in the local labor market (average OEWS location quotient).' },
    { id: 'wage_trade', group: 'labor', label: 'Skilled-trade pay (lower scores higher)', src: 'wage_trade', type: 'pct_inv',
      about: 'Median annual pay for welders, machinists, CNC operators, electricians and industrial mechanics in the local labor market, relative to the U.S. Lower pay means lower labor cost, so it scores higher.' },
    { id: 'wage_logi', group: 'labor', label: 'Driver & warehouse pay (lower scores higher)', src: 'wage_logi', type: 'pct_inv',
      about: 'Median annual pay for heavy-truck drivers and hand material movers in the local labor market, relative to the U.S.' },

    // ---- Taxes, regulation & costs (state level)
    { id: 'tax', group: 'business', label: 'Overall state tax climate', state: 'tf_overall', type: 'rank',
      about: 'Tax Foundation State Tax Competitiveness Index, overall rank (1 = best).' },
    { id: 'corp', group: 'business', label: 'Corporate tax', state: 'tf_corp', type: 'rank',
      about: 'Tax Foundation corporate tax component rank. Accounts for gross receipts taxes in states without a corporate income tax.' },
    { id: 'prop', group: 'business', label: 'Property tax', state: 'tf_prop', type: 'rank',
      about: 'Tax Foundation property tax component rank. Matters for land, buildings and equipment.' },
    { id: 'friendly', group: 'business', label: 'Regulatory & legal climate', state: 'cnbc_bf', type: 'rank',
      about: 'CNBC Top States for Business, “Business Friendliness” category rank, which grades each state’s legal and regulatory framework.' },
    { id: 'regs', group: 'business', label: 'Volume of state regulations', state: 'restrictions', type: 'state_inv',
      about: 'Count of regulatory restrictions (“shall”, “must”, “may not”…) in each state’s administrative code (Mercatus QuantGov State RegData). Fewer scores higher. Not available for Arkansas or West Virginia.' },
    { id: 'rtw', group: 'business', label: 'Right-to-work law', state: 'rtw', type: 'bool',
      about: 'Whether the state has a right-to-work law (26 states).' },
    { id: 'power', group: 'business', label: 'Industrial electricity price', state: 'elec', type: 'state_inv',
      about: 'Average price paid by industrial customers, year to date (EIA Electric Power Monthly). Cheaper scores higher.' },
  ],

  presets: [
    {
      id: 'machine', label: 'Machine shop',
      desc: 'A CNC, fabrication or job shop. It lives on machinists and welders and on manufacturers nearby to sell parts to; rail and ports barely matter.',
      icon: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.9 1.9M16.6 16.6l1.9 1.9M5.5 18.5l1.9-1.9M16.6 7.4l1.9-1.9"/><circle cx="12" cy="12" r="6.6"/>',
      w: { interstate: 6, class1: 0, yard: 0, intermodal: 1, port: 0, air: 2, market: 3,
           trades: 8, logi: 1, metal: 10, whse: 0, lq_metal: 8, lq_elec: 4, wage_trade: 5, wage_logi: 0,
           tax: 3, corp: 3, prop: 3, friendly: 4, regs: 2, rtw: 3, power: 3 },
    },
    {
      id: 'plant', label: 'Manufacturing plant',
      desc: 'A production plant moving heavy inbound and outbound freight. Weighs rail service, power prices, a deep production workforce and taxes on property and income.',
      icon: '<path d="M2.5 20.5v-9l5 3.2v-3.2l5 3.2v-3.2l5 3.2V4h3.5v16.5z"/><path d="M6 17.5h2M11 17.5h2"/>',
      w: { interstate: 7, class1: 6, yard: 3, intermodal: 4, port: 4, air: 2, market: 3,
           trades: 9, logi: 2, metal: 5, whse: 1, lq_metal: 6, lq_elec: 6, wage_trade: 5, wage_logi: 1,
           tax: 4, corp: 5, prop: 5, friendly: 5, regs: 3, rtw: 4, power: 8 },
    },
    {
      id: 'warehouse', label: 'Warehouse / distribution',
      desc: 'A building that serves a regional market by truck. Weighs interstate access, how many people are within a day’s drive, and warehouse labor and pay.',
      icon: '<path d="M2.5 20.5V9.5L12 4l9.5 5.5v11"/><path d="M6 20.5v-6h4v6M14 20.5v-6h4v6"/>',
      w: { interstate: 10, class1: 1, yard: 0, intermodal: 3, port: 2, air: 4, market: 10,
           trades: 1, logi: 8, metal: 0, whse: 6, lq_metal: 0, lq_elec: 1, wage_trade: 0, wage_logi: 6,
           tax: 3, corp: 2, prop: 5, friendly: 3, regs: 1, rtw: 3, power: 2 },
    },
    {
      id: 'hub', label: 'Logistics hub',
      desc: 'A site that moves freight between rail, sea, air and truck. Weighs intermodal terminals, Class I lines, ports and air cargo. (A warehouse reaches customers; a hub switches modes.)',
      icon: '<rect x="3" y="5" width="18" height="8.5" rx="0.5"/><path d="M7.5 5v8.5M12 5v8.5M16.5 5v8.5"/><path d="M2 19h20M5 16.8v4.4M10 16.8v4.4M14 16.8v4.4M19 16.8v4.4"/>',
      w: { interstate: 9, class1: 8, yard: 6, intermodal: 10, port: 7, air: 6, market: 6,
           trades: 1, logi: 9, metal: 0, whse: 8, lq_metal: 0, lq_elec: 1, wage_trade: 0, wage_logi: 4,
           tax: 3, corp: 3, prop: 4, friendly: 3, regs: 1, rtw: 3, power: 1 },
    },
  ],

  // Occupations shown in the county detail (BLS OEWS codes)
  occupations: [
    ['514121', 'Welders, cutters & brazers'],
    ['514041', 'Machinists'],
    ['519161', 'CNC tool operators'],
    ['472111', 'Electricians'],
    ['499041', 'Industrial machinery mechanics'],
    ['533032', 'Heavy truck drivers'],
    ['537062', 'Freight & material movers'],
  ],
};
