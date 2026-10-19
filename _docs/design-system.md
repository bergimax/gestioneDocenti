# Design system

Small on purpose: one stylesheet (`static/css/app.css`), no framework, no build step. Pages are server-rendered Django
templates extending `templates/base.html`; behaviour is HTMX (requests, swaps) and Alpine (local UI state such as a menu
or a highlight). Both are vendored in `static/vendor/`. Users are Italian: all visible text is Italian.

## Tokens

CSS custom properties on `:root`: `--bg`, `--fg`, `--muted`, `--accent`, `--border`, `--surface`. New colors are added as
tokens, not hardcoded in a rule. Status colors are the message palette (`message-success`, `-error`, `-warning`, `-info`).
System font stack, 16px base.

## Layout

- `base.html` gives the header (brand, role-based nav, toggle on mobile), flash messages, `<main>` (max 70rem) and footer
  with change-password and logout. Navigation entries come from `projects.context_processors.navigation` and the
  permission predicates, never from inline role checks.
- Mobile first. The nav collapses below 48rem; wide tables sit in `.table-wrap` and scroll inside it, the page itself
  never scrolls horizontally. Test at phone width.

## Components (class prefixes)

- Generic: `.cards`, `.data-table`, `.form-panel`, `.field` / `.field-error` / `.hint`, `.button`, `.messages`.
- Per area: `tt-` timetable, `av-` availability, `cal-` calendar, `setup-` import wizard, `pw-` public week.
  A new area gets its own prefix.

## Rules

- Accessibility: every control is a real `<button>`, `<a>` or form field with a label; state is not conveyed by color
  alone (a conflict has a "!" flag and hidden text, not only red); icon-only controls have `aria-label`; use
  `.visually-hidden` for screen-reader text. Keyboard must reach every action, including those that also work by drag.
- Forms post without JavaScript first (`method="post"` and `action`), and HTMX (`hx-post`, `hx-target`, `hx-swap`) only
  upgrades them. Errors render next to the field.
- A fragment is a partial template (`_name.html`) returned by the same view that renders the page; out-of-band swaps use
  `hx-swap-oob`.
- Anything a viewer may not see is filtered in the view or queryset, never hidden with CSS or in the template.
- Destructive actions say what they will do and need an explicit confirm step.
- No inline styles with new colors, no CSS frameworks, no icon fonts, no external CDNs (the CSP forbids them).
