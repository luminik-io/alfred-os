# Design language

How the Alfred native desktop app (`clients/desktop`) and the marketing and
docs site (`site/`) look and feel, so a contributor can add a screen or a page
that matches what is already there. This is the visual-language reference. For
the desktop client's product direction and the Slack boundary, see
[`NATIVE_CLIENT.md`](NATIVE_CLIENT.md); for the tab-by-tab operator tour and how
to build installers, see [`DESKTOP_CLIENT.md`](DESKTOP_CLIENT.md). This page is
the visual complement to those two, not a duplicate.

Everything below is pulled from the live token files. If the code and this doc
ever disagree, the code wins. The token sources are:

- Desktop app: `clients/desktop/src/index.css` (the shadcn / Tailwind theme that
  drives most surfaces) and `clients/desktop/src/App.css` (the hand-rolled glass
  system for the older app-shell surfaces).
- Site: `site/src/styles/custom.css` (Starlight docs theme),
  `site/src/styles/marketing.css` (marketing pages), and
  `site/src/styles/cookie-banner.css`.

## Color system

Alfred reads as a calm, steel-blue product with a cyan accent and a violet
spark, on a near-white paper in light and a deep navy in dark. It is not flashy.
The color carries state (ok, warn, error) far more than decoration.

### Desktop app palette

The main app theme (`index.css`) is defined in OKLCH so light and dark stay
perceptually balanced. The load-bearing tokens:

| Token | Light | Dark | Role |
|---|---|---|---|
| `--background` | `oklch(0.965 0.018 238)` cool near-white | `oklch(0.12 0.028 258)` deep navy | App canvas |
| `--foreground` | `oklch(0.17 0.035 255)` | `oklch(0.94 0.016 248)` | Body text |
| `--primary` | `oklch(0.53 0.205 261)` steel violet-blue | `oklch(0.66 0.22 262)` brighter cobalt | Primary action, brand |
| `--accent` | `oklch(0.68 0.15 188)` cyan | `oklch(0.72 0.16 188)` cyan | Secondary accent, highlights |
| `--cobalt` | `oklch(0.53 0.205 261)` | `oklch(0.66 0.22 262)` | Deep blue spot |
| `--chart-3` | `oklch(0.62 0.18 310)` violet | `oklch(0.72 0.18 310)` violet | Third accent in gradients |
| `--ok` | `oklch(0.65 0.15 154)` green | `oklch(0.72 0.14 154)` | Healthy state |
| `--warn` | `oklch(0.7 0.17 78)` amber | `oklch(0.78 0.16 78)` | Caution state |
| `--error` / `--destructive` | `oklch(0.59 0.22 25)` red | `oklch(0.72 0.18 24)` | Failure state |

Primary buttons and the brand mark use a `primary` to `accent` gradient (the
blue-to-cyan sweep you see on the send button and the compose welcome mark). The
agent cards each carry a per-agent accent (`--agent-accent`, defaulting to
`--primary`) that tints their border, glow, and selection ring, so the roster
reads as a constellation of distinct agents rather than identical tiles.

A second, older token set lives in `App.css` for the legacy app-shell surfaces.
It uses a warm amber accent (`--accent: #f7b344` in dark, `#b87518` in light)
rather than cyan. Both files are imported, with `index.css` loaded last, so the
shadcn OKLCH theme above is the dominant one for new work. Prefer the
`index.css` tokens when you add a surface; reach for the `App.css` amber tokens
only when you are extending an existing `.app-shell` component that already uses
them.

### Site palette

The site (`custom.css`) keys off a `data-theme` flag and ships both modes:

| Token | Dark | Light | Role |
|---|---|---|---|
| `--sl-color-bg` | `#0d1322` | `#f7f9fc` | Page background |
| `--sl-color-accent` | `#4a78ff` | `#2855c8` | Links, accent |
| `--alfred-ok` | `#2dd4a7` | `#087a5d` | Healthy |
| `--alfred-warn` | `#f5a524` | `#8f5600` | Caution |
| `--alfred-alert` | `#ff5d6c` | `#d92d3c` | Failure |

Note the deliberate contrast choice in light mode: the link accent darkens from
`#4a78ff` to `#2855c8`. The code comment is explicit about why ("darkened for AA
contrast on white. Do NOT use #4a78ff here."). When you pick a color for text or
an interactive element on a light surface, check it against the background for at
least AA contrast rather than reusing the brighter dark-mode value.

### Light and dark behavior

- Desktop app: `useTheme.ts` stores the choice in `localStorage` under
  `alfred-theme`, defaults to dark, and applies it by setting `data-theme` on the
  document root and toggling a `.dark` class. The `:root` block is the light
  theme; the `.dark` block overrides it.
- Site: the Starlight and marketing layouts swap on `data-theme` the same way,
  with the dark block as `:root` and a `:root[data-theme="light"]` override.

When you add a token, define it in both the light base and the dark override.
Do not hard-code a hex or OKLCH value in a component; reference a token so both
themes stay covered.

## Typography

The desktop client and the site share one body face (Quicksand) but have their
own display and mono faces. Each surface owns its own font tokens; they are not a
single shared stack. Both bundle their fonts locally through `@fontsource` (no
runtime call to a font CDN).

### Desktop client typography

One display face, one body face, one mono face, defined in
`clients/desktop/src/index.css`:

| Use | Family | Where |
|---|---|---|
| Display / headings | **Instrument Sans** (variable) | `--font-heading`, `--font-display` |
| Body / UI | **Quicksand** (variable, 400 to 700) | `--font-sans`, `--font-body` |
| Mono / code / labels | **Fragment Mono** | `--font-mono` |

This is the operator font directive of 2026-06-13 and it must not be reverted.
A guard test (`clients/desktop/src/test/directive-guards.test.ts`) reads
`index.css` and fails if these tokens drift, so the directive holds for the
client only. The full stacks, straight from the code:

- `--font-display` / `--font-heading`: `"Instrument Sans Variable",
  ui-sans-serif, system-ui, sans-serif`
- `--font-body` / `--font-sans`: `"Quicksand Variable", "Instrument Sans
  Variable", ui-sans-serif, system-ui, sans-serif`
- `--font-mono`: `"Fragment Mono", ui-monospace, SFMono-Regular, Menlo, Monaco,
  Consolas, monospace`

How they are used in practice:

- Instrument Sans carries headings, large display numbers, card titles, metric
  values, and the agent monogram marks. It gives the product its clean, precise
  headline voice.
- Quicksand is the default for body copy and most UI text. It is the rounded,
  friendly base that keeps the dense dashboards readable.
- Fragment Mono is for code, log lines, agent codenames, timestamps, and small
  uppercase labels where a fixed width and a technical feel help.

### Site typography

The site does not use the client's Instrument Sans or Fragment Mono. It has its
own tokens in `site/src/styles/marketing.css`, and the Starlight docs theme
(`site/src/styles/custom.css`) sets `--sl-font` to Quicksand. Quicksand is the
only face the two surfaces share. The marketing stacks, straight from the code:

- `--font-display`: `"Space Grotesk Variable", "Quicksand", system-ui,
  sans-serif`
- `--font-body`: `"Quicksand", system-ui, sans-serif`
- `--font-mono`: `"JetBrains Mono", ui-monospace, "SF Mono", Menlo, monospace`

In practice Space Grotesk carries the marketing headlines and display numbers,
Quicksand carries body copy and the docs UI, and JetBrains Mono carries the
log-style status strips, metric rows, and code blocks. The marketing display
sizes are tokenized and scale down on narrow screens: `--text-display-xl` is
72px on desktop and steps to 44px then 34px at the small breakpoints, so
headlines never overflow.

## Glass and surfaces

Alfred leans on layered, slightly translucent panels over a soft gradient
backdrop, not flat cards on a flat page.

- The page background is built from stacked gradients plus a faint 88px grid
  (desktop app) or 84px grid (site), so the canvas has texture without noise. In
  dark mode the gradients are deeper navy with blue, cyan, and violet glows in
  the corners.
- Panels use the `.alfred-glass` treatment: a translucent `--card` fill, a hair
  border, an inset top highlight (`inset 0 1px 0 rgba(255,255,255,...)`), a soft
  drop shadow, and `backdrop-filter: blur(24px) saturate(145%)`. The blur and
  saturate are also exposed as `--glass-blur` (24px) and `--glass-saturate`
  (145%) so the roster cards match the panels exactly.
- Surfaces come in tiers (`--surface`, `--surface-2`, `--surface-3`, `--glass`,
  `--glass-strong`) so nested panels can step in opacity and read as depth.
- Corner radius is token-driven. The app base radius is `--radius: 0.5rem` in
  the shadcn theme (with `sm` / `md` / `lg` / `xl` and larger steps derived from
  it), `7px` in the App.css system, and `8px` on the site. Reuse the token; do
  not pick a one-off radius.

When you build a new panel, start from `.alfred-glass` (or the existing
`.compose-chat-panel` / `.agents-v2` family) rather than a plain `background` and
`border`, so it sits in the same depth system.

## Motion

Motion is small, fast, and purposeful. Nothing bounces or slides far.

- Hover and selection transitions are short: 120ms on the site, 150 to 160ms on
  app cards and rows, on an `ease` curve. Agent cards lift 3px on hover
  (`translateY(-3px)`) and rows nudge 2px sideways.
- The agent roster rises on mount: each card animates in with a small
  `translateY(4px)` to `0` over 180ms, staggered by index and capped at the
  sixth child so a long roster still settles quickly (the `alfred-rise`
  keyframe).
- The site uses a gentle `m-pulse` for live-status dots and an `m-reveal`
  fade-and-rise for sections as they enter the viewport.

### prefers-reduced-motion

Every motion path has a reduced-motion guard, and you must add one to anything
new:

- In the app, `@media (prefers-reduced-motion: reduce)` neutralizes the card
  lift, the row slide, and the staggered mount entry (`transform: none;
  transition: none;` and `animation: none` on the rise). The selection and hover
  color and border changes still apply, so the affordance survives without
  movement.
- On the site, the same query stops the status-dot pulse and turns the
  `m-reveal` sections fully visible with no transform or transition.

The rule: motion is an enhancement, never the only signal. If a user prefers
reduced motion, the interface must still show state through color, border, and
layout.

## Accessibility

- **Contrast.** Pick colors against their real background and target at least AA.
  The site already encodes this (the `#2855c8` light-mode link accent is the
  AA-safe version of `#4a78ff`); do the same when you introduce a color.
- **Visible focus.** Keyboard focus shows a 2px solid accent outline with a 2px
  offset (`:focus-visible` in `App.css`, `.m-*:focus-visible` on the site).
  Mouse focus is suppressed via `:focus:not(:focus-visible)`. Do not remove focus
  outlines; if you restyle focus, keep a clearly visible ring.
- **Real controls.** Interactive things are real `<button>` / `<a>` elements, not
  clickable `<div>`s. The agent cards and roster rows are buttons specifically so
  screen readers announce them as actionable and keyboard users can tab to them.
  Keep that pattern: if it does something on click, it should be a real control.
- **Pointer affordance.** Enabled buttons and `[role="button"]` get
  `cursor: pointer`; disabled buttons read as `wait` and dim to ~0.72 opacity.

## Quick checklist for a new surface

1. Use the token colors (`index.css` for the app, `custom.css` /
   `marketing.css` for the site). Define new tokens in both light and dark.
2. Use the surface's own font tokens, never a hard-coded family. On the desktop
   client that is Instrument Sans headings, Quicksand body, Fragment Mono code;
   on the site it is Space Grotesk display, Quicksand body, JetBrains Mono code.
3. Build panels from the glass system and reuse the radius token.
4. Keep transitions short (120 to 200ms) and add a `prefers-reduced-motion`
   guard.
5. Use real buttons and links, keep the visible focus ring, and check contrast
   against the actual background.
