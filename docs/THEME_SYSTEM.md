# Desktop Theme System

Alfred Desktop uses a two-axis theme model:

- `data-theme` selects the named palette: `alfred` or `linear`.
- `.dark` / `.light` selects the mode inside that palette.

The base `:root` block in `clients/desktop/src/index.css` is the complete
token contract and also the Alfred dark default. Every named theme and mode
must define the same color-token set so components never fall back to an
undefined `var()`.

## Glass And Flat Surfaces

Use glass tokens for app chrome: sidebar, command palette, dialogs, and hero
surfaces.

Use flat surface tokens for dense work surfaces: lifecycle columns, lists,
cards, logs, and tables.

`--glass-blur` lets each named theme dial the amount of blur without rewriting
component CSS. Alfred can be more cinematic. Linear Crisp is flatter, denser,
and more neutral.

## Guardrail

`clients/desktop/src/test/theme-tokens.test.ts` reads `index.css` and fails if a
theme block omits any required color token from the base set. When adding a new
theme, add a complete block and update the test's theme list.
