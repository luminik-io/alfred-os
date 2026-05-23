# Alfred design system

Single source of truth for typography, color, layout, and motif on alfred.luminik.io. The site is a quiet control surface for an autonomous engineering fleet, not a marketing brochure. Every decision below points at one of three goals:

1. Distinct from the default "dark navy + Quicksand + blue accent" template every dev tool ships.
2. Reinforces Alfred's promise: the fleet ships while you are not at the keyboard.
3. Honors the Luminik voice rules (no em-dashes, no banned vocabulary, specific over vague, ≤30-word subheads).

## Concept

Operations Room. The site looks and feels like a watch desk for a fleet that runs without you. Status indicators, log-style strips, sharp panels, generous quiet. The reader should leave with the sense that Alfred is calmly working in the background.

## Color tokens

```css
--alfred-black:           #0A0E14;  /* warm near-black canvas */
--alfred-surface:         #11161F;  /* card / panel surface */
--alfred-surface-2:       #161D2A;  /* hover / focused panel */
--alfred-border:          #1F2937;  /* subtle separation */
--alfred-border-strong:   #2A3548;  /* card outlines */
--alfred-mute:            #6B7A8F;  /* muted body, metadata */
--alfred-text:            #C5D0E0;  /* default body */
--alfred-text-bright:     #F2F6FF;  /* primary headings */

/* Accents: status palette */
--alfred-accent:          #00E5C7;  /* primary; healthy / live / go */
--alfred-accent-soft:     #1B3A37;  /* tinted background for accent badges */
--alfred-warn:            #F4B43E;  /* amber; status warning, also exists in logo */
--alfred-error:           #FF5A6B;  /* red; status alert */

/* Brand */
--alfred-blue:            #5789FF;  /* logo emblem only, sparingly */
```

Light-mode variant is deferred. The site is dark-first by intent.

## Typography

```css
--font-display: "Space Grotesk", "Inter", system-ui, sans-serif;
--font-body:    "Inter", "Quicksand", system-ui, sans-serif;
--font-mono:    "JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace;
```

Scale (display 1.25 step, body 1.125 step):

| Token | Size | Use |
|---|---|---|
| `--text-display-xl` | 84px / 1.02 / 700 | Hero H1 only |
| `--text-display-lg` | 64px / 1.05 / 700 | Page H1 |
| `--text-display-md` | 44px / 1.1 / 700 | Section H2 |
| `--text-h2` | 32px / 1.2 / 600 | Subsection |
| `--text-h3` | 22px / 1.3 / 600 | Card titles |
| `--text-body-lg` | 19px / 1.55 / 400 | Lede paragraphs |
| `--text-body` | 16px / 1.6 / 400 | Body |
| `--text-body-sm` | 14px / 1.55 / 400 | Captions |
| `--text-mono` | 14px / 1.5 / 500 | Log lines, code, status strips |
| `--text-eyebrow` | 12px / 1 / 600 / 0.18em letterspacing / uppercase | Eyebrows |

Headings always use display font. Body and mono never mixed within a sentence.

## Layout

- Max content width 1180px, gutters 32px on tablet+, 20px on mobile.
- Card radius 8px (sharp, not pill).
- Border `1px solid var(--alfred-border-strong)`.
- Hover state: lift to `--alfred-surface-2`, no scale transform.
- 8px grid spacing, with prominent multipliers at 16, 24, 32, 48, 64, 96, 128.

## Motifs

### 1. Status dots

Inline `●` glyph carries semantic state. Always paired with adjacent label.

```html
<span class="status status--live">● Live</span>
<span class="status status--warn">● Pending review</span>
<span class="status status--idle">● Idle</span>
```

```css
.status { font: var(--text-mono); letter-spacing: 0.04em; }
.status--live  { color: var(--alfred-accent); }
.status--warn  { color: var(--alfred-warn); }
.status--idle  { color: var(--alfred-mute); }
.status--error { color: var(--alfred-error); }
```

### 2. Log strip

Each page renders a representative-but-static log strip near the footer. Mono font, faded text color, fixed-width characters. Acts as the brand's signature moment.

```text
[12:04:11] drake   triaged 4 issues from /specs   ● green
[12:04:18] lucius  claimed luminik-backend#247    ● green
[12:04:21] lucius  worktree opened   ~/.alfred/wt/12042118/
[12:18:09] lucius  PR luminik-backend#1247 opened  ● green
[12:18:33] ras-al  reviewed PR#1247   2 nits       ● amber
```

### 3. Code-style metric rows

Vertical alignment, mono, with semantic color on the metric value.

```text
agents     ●  21
repos      ●   9
firings    →  18m ago
errors     ●   0
```

### 4. Quiet animation

No carousels, no parallax. The only motion permitted: a 0.6s ease-in fade on the status dot when the page is idle (suggests heartbeat). Respect `prefers-reduced-motion`.

## Component patterns

- `<Hero>` — eyebrow + display-xl headline + body-lg sub + primary CTA + secondary text-link CTA.
- `<MetricStrip>` — 3-5 mono-aligned metric rows in a single card.
- `<JobStory>` — card showing one buyer-side outcome ("a Drake firing files four scoped issues") with a tiny log excerpt under it.
- `<CodeBlock>` — JetBrains Mono, scrollable, prompt prefix `$` rendered in `--alfred-accent`.
- `<StatusStrip>` — fixed-width log-style strip footer-adjacent.

## Voice on the site (binding)

- No em-dashes. Use period, comma, parens, or semicolon.
- No banned vocabulary: seamless, unlock, leverage, transform, synergy, cutting-edge, revolutionize, streamline.
- No "X. Y. The Z." rhythm. No "X, not Y" inside a single clause. No aphoristic punchlines.
- Specific over vague. Cite real names (Lucius, Drake, Ras al Ghul, Batman, Bane, Nightwing, agent-cleanup), real labels (`agent:implement`, `agent:in-flight`, `agent:pr-open`, `agent:done`), real intervals ("every 20 minutes").
- Hero subhead ≤ 30 words. Always.
- Cover-the-name test: every hero must still describe a specific category and value when "Alfred" is hidden.

## Anti-list (do not do)

- No carousels.
- No "client logo" strip we do not have real logos for.
- No fake testimonials.
- No CTAs ending in "!".
- No "AI-powered" anywhere on the site.
- No "Get started in 30 seconds" claims (install is honestly 30 min minimum).
- No fake screenshots in the hero. Use real log output, real GitHub issue bodies, real Slack message text.
