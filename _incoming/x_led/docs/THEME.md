# V Tech house theme — extracted from the owner's real dashboard

Source of truth: `/mnt/user-data/uploads/RestockerLocal/dashboard_redesign_v3.html`
(his own "Abexilas Economy Hub" redesign). READ THAT FILE before restyling anything.
Match it. Do not invent a new look.

## Tokens (copy verbatim)

```css
:root{
  --font-data:'IBM Plex Mono',ui-monospace,Menlo,monospace;
  --font-ui:'Space Grotesk',system-ui,sans-serif;
  --bg:#080808;--surface:#0f0f0f;--panel2:#151515;--border:#1E1E1E;--border-strong:#2A2A2A;
  --text:#F4F4F4;--text-body:#B4B4B4;--muted:#6a6a6a;--faint:#3f3f3f;
  --green:#22FF7A;--green-dim:#0f7a3a;--accent:#22FF7A;--red:#FF4D4D;
  --amber:#F5A623;--blue:#4A9EFF;--purple:#B47FFF;--nether:#FF6B35;
}
```

Fonts loaded from Google Fonts:
`IBM+Plex+Mono:wght@400;500;600` and `Space+Grotesk:wght@300;400;500;600;700`.

## Non-negotiable rules of this look

1. **No border-radius. Anywhere.** Every panel, button, input, tile and pill is a sharp
   rectangle. The only round things are status dots and donut charts. This single rule
   is most of the difference between his look and generic AI-dashboard output.
2. **Every number is `--font-data` with `font-variant-numeric: tabular-nums slashed-zero`.**
   Numbers never wear the UI font.
3. **Body background is `#080808` with a dot grid:**
   `background-image:radial-gradient(#151515 .5px,transparent .5px);background-size:24px 24px`
4. **Section and tile headers are 10–11px, UPPERCASE, `letter-spacing:.08–.1em`,
   `color:var(--muted)`.** Never a large bold heading over a card.
5. **One accent: `#22FF7A`.** Green means positive/active/primary-action. Red `#FF4D4D`
   negative. Amber `#F5A623` warning/held. Blue/purple/nether are category tints only.
   No gradients except the thin bar fill `linear-gradient(90deg,var(--green),#17b558)`.
6. **Buttons:** solid `--accent`, black text, 11px, weight 600, UPPERCASE,
   `letter-spacing:.05em`, no radius. Ghost variant = transparent + 1px `--border`.
7. **NO EMOJI.** Icons are inline SVG, 14px, `stroke-width:1.7`, `fill:none`,
   `stroke:currentColor`, `opacity:.7` when inactive. He uses lucide-style line icons.
8. **Tables:** numerics right-aligned, first column left, `th` uppercase 11px muted and
   sortable-looking, 1px `--border` row separators, row hover `--panel2`.
9. **Section rule:** `.section-h::after{content:"";flex:1;height:1px;background:var(--border)}`
   — a label followed by a hairline to the right edge.
10. **Density is high.** Base font-size 13px, line-height 1.5, tile padding 18px 20px,
    grid gap 14px. Not airy. Not a lot of whitespace.
11. Bento layout is `grid-template-columns:repeat(12,1fr)` with tiles spanning 5/7/12.
12. Page transitions: `@keyframes f{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}`
    at `.25s`. Nothing bouncier than that.

## Slop to strip out

- rounded corners, soft shadows, glassmorphism, blur panels (except the 54px sticky header
  which does use `backdrop-filter:blur(10px)` — that one is his)
- emoji as icons or bullets
- indigo/violet-on-dark "SaaS" palettes, multi-hue gradients, glowing gradient buttons
- oversized hero headings, generous whitespace, centered marketing copy
- pill-shaped badges with rounded-full
- "Powered by" footers, fake logos, lorem filler
- decorated empty states — an empty state is EMPTY, one muted line of text at most
