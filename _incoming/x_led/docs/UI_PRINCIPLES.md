# Non-slop UI — the rules, and why

Two sources, merged and reconciled against John's own dashboard (`THEME.md`).
Read `THEME.md` first for the concrete tokens; this file is the *reasoning* that
generalises to screens THEME.md doesn't cover.

Sources: the AI-design-tell catalogue at claudecodehq.com/playbooks/unslop-ui, and
Paul Wallas on designing for data density. Where they conflict with John's real
dashboard, John's dashboard wins — it is the brand reference the first source says
to anchor to.

---

## Part 1 — The AI tells, named

Each of these is a *specific* pattern, not a vibe. If the output contains one, it
reads as generated. Ranked by how loudly they announce themselves.

| # | Tell | Concrete signature | Fix |
|---|---|---|---|
| 0 | **Tasteful default** | cream bg (`#faf8f5`, `#f5f1e8`), a fashion serif (Instrument Serif, Fraunces, Playfair), deep green primary (`#15573a`) | Any two of the three together is the tell. Anchor to a real brand instead. |
| 1 | **Untouched shadcn** | `rounded-lg border bg-card text-card-foreground shadow-sm`, `baseColor: slate`, `--radius: 0.5rem` left at default | Override the tokens *before* building, not after |
| 2 | **AI purple** | `#6366f1`, `#7c3aed`, `#8b5cf6`, `#a855f7`; any `--primary` at HSL hue 255–280 | Non-violet brand colour |
| 3 | **Gradients everywhere** | `bg-gradient-to-* bg-clip-text text-transparent`, purple→blue | Solid fills. At most one restrained accent gradient. Never on running text |
| 4 | **Animation spam** | `hover:scale-105` on every card, `whileInView` fade-ups on every section | Motion communicates state or it doesn't exist. Honour `prefers-reduced-motion` |
| 5 | **Uniform round corners** | `rounded-2xl`/`rounded-3xl` on everything, `rounded-full` buttons | A small deliberate radius scale — or, for John, none at all |
| 6 | **Unprompted neon glow** | `shadow-[0_0_*]`, `text-cyan-400` on `bg-slate-950` | Dark mode reads via contrast and spacing, not luminosity |
| 7 | **Emoji as icons** | emoji in headings, feature titles, list bullets — rocket, sparkles, lightning, fire, check | Real icon set (Lucide/Phosphor SVG) or no icon |
| 8 | **Framework default fonts** | Inter / Geist / system-ui as the only face | A stated pairing with a stated reason |
| 9 | **Centered hero → 3 cards → CTA** | `text-center`, `text-6xl` headline, then `md:grid-cols-3` of identical icon+title+blurb cards | Break symmetry. Real data and real screenshots instead of icon cards |
| 10 | **Almost-aligned** | `p-3` next to `p-7` next to `mt-[37px]`; text clipping; every section equal weight | One spacing scale, real hierarchy, test with long content |
| 11 | **Marketing voice** | "Transform your…", "Supercharge", "Effortlessly", "reimagined"; undraw illustrations | Say what the thing actually does |

**Explicitly cleared — do not "fix" these:** dark mode itself, bento grids, mesh
backgrounds, and Tailwind/shadcn as tools. The *defaults* are the tell, not the library.

## Part 2 — Density, because this product is numbers

John's product is a ledger. Cards-with-whitespace is the wrong genre for it.

- **4px grid.** Padding 4 / 8 / 12, not 16 / 24.
- **14px body, 20px line-height. 12px labels, 16px headers.** (John runs 13px — even tighter.)
- **Buttons 32–36px tall.** Compact, not tiny.
- **Two type colours maximum**: primary for active content, muted for support. A third
  colour must *mean* something (positive / negative / held).
- **Tables beat cards** for anything relational or repeating. Cards are for
  heterogeneous objects, not for rows.
- **Freeze headers and the first column** on long tables. Sortable columns. Paginate.
- **Numbers right-align, labels left-align, always monospace with tabular figures.**
  Columns of numbers must be scannable by eye without reading them.
- **Dates human, not ISO**: "12 Jun 2026", "3 weeks ago", "in 2 days". Never `2026-06-12T…`.
- **Progressive disclosure**: secondary controls behind "…", not spread across the surface.
- **Top-left reads first, bottom-right last.** Put the number he opens the page for
  in the top-left, and pass the 5-second glance test.
- **Every pixel justifies itself.** No decoration, and no decorated empty states —
  an empty state is one muted line.

## Part 3 — Rules specific to a money product

Not in either source; these come from John's own bugs.

- **A figure with no unit and no timeframe is a bug.** "12,500" alone means nothing —
  "12,500 held · 2 positions" does.
- **Held / available / debt need distinct colours** and must never be summed into one
  "balance" number in the UI. The whole point of escrow is that they are different.
- **Anything irreversible shows the figures it is about to move, in the same view as
  the button.** Not on the previous screen, not in a tooltip.
- **Indicative numbers must be labelled indicative** where they float (pari-mutuel odds).
- **Real names over internal ids** anywhere a user looks — GreyHames, not `main`.
- **Show the state machine when the user is inside one.** A payout run that is 60/200
  rows in should say so, with what happens if it stops.
