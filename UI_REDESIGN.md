# UI Redesign: "Calm" (draft)

Goal: a minimal, quiet panel. Fewer nav items, almost no text on the page, help lives behind an "i" icon.
Stack stays the same: FastAPI + Jinja + HTMX, hand-written CSS, no JS framework, no CDN (self-hosted).
Routes and URLs stay the same. Only templates, CSS, and a little JS change. No logic changes.

## 1. Problems today

- 18 links in the top bar (Dashboard, Jobs, Approvals, Inbox, Review, Runs, Profile, Profile history, Playbook, Trends, Reports, Learning, Upwork, Setup, Settings, Email, ...).
- Long paragraphs of help text on most pages (setup, approvals, job detail, dashboard).
- Every page looks equally loud. No clear "what do I do now".

## 2. Principles

1. One page = one job. The page title says what it is. One primary button says what to do.
2. Text rule. Visible text is only: page title, section title, labels, values, button labels, one-line empty states, and warnings.
3. Everything else goes into an "i" tooltip: what it is + what to do. Max 2 short sentences (about 160 chars).
4. Safety text is NEVER hidden in a tooltip: dry-run state, kill switch, Connects cost and balance on Approve, errors, "this goes live on Upwork" notes, delete warnings.
5. Numbers before words. Show a number, a pill, or a small chart instead of a sentence.
6. Calm by default, loud only for action: one accent color, used only for the main button, the active nav item, and links.

## 3. Navigation (5 items + gear)

Top bar, 56px high, left to right:

`[logo] Home  To do (3)  Jobs  Inbox  Growth          [Dry-run pill] [gear menu]`

| Nav item | Pages inside (tabs at top of the page) |
|---|---|
| Home | `/` dashboard |
| To do (badge) | `/approvals` (renamed "To do" in the UI only) |
| Jobs | `/jobs`, `/jobs/<id>`, tab "Review" -> `/review` |
| Inbox | `/inbox` |
| Growth | tabs: Profile `/context`, History `/profile/versions`, Playbook `/playbook`, Trends `/trends`, Reports `/reports`, Learning `/learning` |
| Gear menu | Settings `/settings`, Email `/settings/email`, Upwork `/upwork`, Runs `/runs`, Setup `/setup`, Log out |

- Tabs are a shared macro: a row of text links under the page title, active one underlined in accent.
- The gear opens a small dropdown (pure HTML `<details>`), not a new nav row.
- Mobile (< 720px): nav becomes a bottom bar with 5 icons + labels; gear stays top right.
- Status pills in the top bar: `Dry-run` (amber) when dry-run is on, `Stopped` (red) when kill switch is on. Click goes to `/settings`.

## 4. The "i" help icon

- Jinja macro in `app/templates/_macros.html`: `{{ info("jobs.score") }}`.
- Help text lives in ONE file: `app/help.toml` (key -> text). No help text in templates.
- Markup: `<button type="button" class="info" aria-describedby="tip-x">i</button><span role="tooltip" id="tip-x">...</span>`.
- Shows on hover, on keyboard focus, and on tap (mobile). Esc closes. Tiny JS (`app/static/info.js`, under 40 lines) only for tap and Esc; hover and focus work with CSS alone.
- Look: 16px circle, 1px muted border, "i" in muted color; accent on hover. Tooltip: dark surface, 13px text, max width 280px, 8px padding, radius 8, small arrow, fade 120ms (no motion if `prefers-reduced-motion`).
- Place: right after a page title, a section title, a stat label, or a table header. Never inside a sentence.
- A test fails if a key used in a template is missing from `help.toml`, or a key in `help.toml` is unused.

Text style for tooltips: plain words, short sentences, active voice. Pattern: "What it is. What to do."
Example `jobs.score`: "How well this job fits you, 0 to 100. Open jobs above 70 first."

## 5. Look and feel

Font: Inter (variable, OFL license) bundled in `app/static/fonts/`, fallback `system-ui`. Mono: `ui-monospace`.
Sizes: 13 (small), 14 (body), 16 (section title), 20 (page title), 28 (big stat). Weights 400 / 500 / 600 only.
Spacing: 4px base, use 8 / 12 / 16 / 24 / 32 / 48. Radius 10 (cards), 8 (buttons, inputs), 999 (pills).
Shadow: none on cards; 1px border instead. One soft shadow only for menus and tooltips.
Max content width 1120px, centered. Page padding 24px (16px on mobile).
Icons: Lucide (ISC license), only the ~20 icons used, as an inline SVG sprite in `app/static/icons.svg`. 16px, stroke 1.75.

### Palette (CSS variables in `app/static/app.css`, light + dark via `prefers-color-scheme`, plus a manual toggle saved in a cookie)

| Token | Light | Dark |
|---|---|---|
| `--bg` | `#F7F7F5` | `#0F1012` |
| `--surface` | `#FFFFFF` | `#17181B` |
| `--surface-2` | `#F0F0EC` | `#1F2024` |
| `--border` | `#E7E5E0` | `#2A2B30` |
| `--text` | `#18181B` | `#ECECEE` |
| `--muted` | `#71717A` | `#A1A1AA` |
| `--accent` | `#0F766E` (deep teal) | `#2DD4BF` |
| `--accent-hover` | `#115E59` | `#5EEAD4` |
| `--accent-soft` | `#E6F2F0` | `#0E2A27` |
| `--accent-text` | `#FFFFFF` | `#04201C` |
| `--ok` / soft | `#15803D` / `#E8F5EC` | `#4ADE80` / `#11281A` |
| `--warn` / soft | `#B45309` / `#FDF3E2` | `#FBBF24` / `#2E2208` |
| `--bad` / soft | `#B91C1C` / `#FCEBEB` | `#F87171` / `#2E1214` |
| `--info` / soft | `#1D4ED8` / `#EAF0FD` | `#93B4FF` / `#16213D` |

Fit pills: HIGH = ok colors, MID = info colors, LOW = muted on surface-2.
All text/background pairs must pass WCAG AA (4.5:1 body, 3:1 large). Check with a small script in tests.

## 6. Components (one place: `_macros.html` + `app.css`)

- `page_header(title, help_key, action)`: title + "i" left, one primary button right.
- `tabs(items, active)`.
- `card(title, help_key)`: white surface, border, 16px padding, title 16/600.
- `stat(label, value, delta, help_key)`: big number, small label, small green/red delta arrow.
- `pill(kind, text)`.
- Buttons: `primary` (accent fill), `secondary` (border only), `ghost` (text only), `danger` (red fill, only for delete). Height 36px. Loading state via `htmx-request` class.
- Table: no zebra, 1px row lines, 44px rows, header 12px uppercase muted, row hover surface-2, whole row clickable when it opens a detail page.
- Empty state: one icon, one line, one button. Example: "No jobs yet." [Run now].
- Notice (toast): from `?msg=`, top right, auto-hide 4s, except errors stay.
- Forms: label above input, help in "i" next to the label, error text in red under the input.

## 7. Pages

Every page: `page_header` + content. No intro paragraph.

- **Home**: row of 4 stats (New good jobs today, Waiting in To do, Connects left, Claude cost this month). Below: "To do" card (top 5 items, button "Open all"), "Best jobs today" card (top 5 rows), small funnel chart, keyword rank sparkline. Last run time + Run now in the header action.
- **To do**: one list, newest first. Each row: kind icon, title, short before -> after or amount, buttons Approve / Reject. Filter chips by kind. "Approve all small changes" as a secondary button at the top, only when there are some. Connects cost and "goes live on Upwork" stay visible on rows that write.
- **Jobs**: filter bar in one row (Fit, Budget, Can afford, search). Table: Fit pill, Title, Budget, Client (stars + spend), Posted, Score. No descriptions in the list.
- **Job detail**: two columns. Left: job (title, budget, client facts as small key/value rows, description collapsed after 8 lines). Right (sticky): "Apply" card: draft editor, Connects cost + balance (visible), questions, then Prepare -> Approve. Disabled buttons show the reason in the tooltip of the button.
- **Review**: paste box + Check button; result as score badge + a short list of issues; diff in a collapsed block.
- **Inbox**: two panes. Left: rooms list (name, last line, time, unread dot). Right: thread + "Draft reply" button + editor. Mobile: one pane at a time.
- **Growth / Profile**: USER.md editor, chars used as a small meter, Rebuild button.
- **Growth / History**: timeline of versions; click one to see the diff; Roll back as secondary.
- **Growth / Playbook**: active test card at top (tactic, days left as a progress bar, block reason as a pill); tactics list below.
- **Growth / Trends**: 2 charts and one table. Chart titles have "i".
- **Growth / Reports**: list (kind pill, date, title) -> report page with clean markdown typography.
- **Growth / Learning**: list of changes (date, message, +/- lines), click to see diff, Undo as ghost button; Run buttons in a small "Jobs" menu in the header.
- **Settings**: left side list of sections, right side form for the chosen section. Dry-run and kill switch as large toggles at the top. Danger zone (export, delete all) at the bottom in a red-bordered card.
- **Setup wizard**: one step per screen, step dots at top, Back / Next at bottom, one short title per step, help in "i".
- **Login**: centered card, logo, 2 fields, 1 button. Nothing else.

## 8. Plan for /ship (phases)

U1. Foundation: tokens + palette + dark toggle, Inter font, icon sprite, `_macros.html` components, `info()` macro + `app/help.toml` + `info.js`, help-key test, contrast test. Effort: medium.
U2. Shell: new top bar (5 items + gear + status pills), tabs macro, mobile bottom bar, `To do` label, nav test (max 5 items + gear). Effort: medium.
U3. Home + To do. Effort: medium.
U4. Jobs, Job detail, Review. Effort: high (job detail has write actions; keep all safety text visible).
U5. Inbox + Growth pages. Effort: medium.
U6. Settings, Setup wizard, Login, Upwork, Email, Runs. Effort: high (settings has delete-all and dry-run).
U7. Polish: move every leftover paragraph into `help.toml`, screenshots of every page at 1280 and 390 wide in light and dark, fix spacing. Effort: medium.

Done when (each phase): `.venv/bin/pytest -q -n auto` pass, ruff pass, screenshots of changed pages; at end of the last phase also the Docker check.
Extra test: a "text budget" test renders each page with fake data and fails if any `<p>` outside `.notice`, `.warning`, `.empty` is longer than 120 chars.

## 9. Decisions for the human

- D-UI1: Rename "Approvals" to "To do" in the UI (URL stays `/approvals`). Default: yes.
- D-UI2: Bundle Inter font (about 300 KB) or use system font only. Default: bundle Inter.
