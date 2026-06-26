# DESIGN.md

## Design system for u-ecom-scraper

### Register: Operations console with edge

Dark, information-dense, mission-control aesthetic. One accent color. Strong typographic hierarchy. Depth through layering.

---

## Color

Dark base. Three layers of surface elevation. One accent for action and live states. Semantic colors for status.

### Surfaces (elevation layers)

| Token | Hex | Usage |
|-------|-----|-------|
| `bg-void` | `#0B0F17` | Page background — deepest layer |
| `bg-base` | `#111827` | Primary surface (cards, panels) |
| `bg-raised` | `#1A2332` | Elevated elements (hover, active panels, dropdowns) |
| `bg-overlay` | `#232E42` | Modals, popovers, top of stack |
| `border-subtle` | `#1E293B` | Default borders, dividers |
| `border-strong` | `#334155` | Emphasized borders, focus-adjacent |

### Text

| Token | Hex | Usage |
|-------|-----|-------|
| `text-primary` | `#F1F5F9` | Body text, headings — primary |
| `text-secondary` | `#94A3B8` | Labels, metadata, secondary info |
| `text-muted` | `#64748B` | Placeholder, disabled, tertiary |
| `text-accent` | `#22D3EE` | Links, accent-colored text |

### Accent (the one color with intent)

| Token | Hex | Usage |
|-------|-----|-------|
| `accent` | `#22D3EE` | Primary actions, active states, live indicators |
| `accent-hover` | `#06B6D4` | Hover on accent elements |
| `accent-muted` | `#0E7490` | Accent backgrounds (badges, subtle fills) |
| `accent-glow` | `rgba(34, 211, 238, 0.15)` | Glow/halo on live elements |

Cyan. Reads as "active/system/live" — distinct from green (success) and the typical blue-purple SaaS palette. Pops hard against the dark void.

### Status (semantic)

| Token | Hex | Usage |
|-------|-----|-------|
| `success` | `#34D399` | Completed jobs, successful phases |
| `success-bg` | `rgba(52, 211, 153, 0.12)` | Success badges, fills |
| `warning` | `#FBBF24` | Paused, awaiting approval, budget warnings |
| `warning-bg` | `rgba(251, 191, 36, 0.12)` | Warning badges, fills |
| `danger` | `#F87171` | Failed jobs, errors, rejected |
| `danger-bg` | `rgba(248, 113, 113, 0.12)` | Danger badges, fills |
| `info` | `#60A5FA` | Informational, in-progress |
| `info-bg` | `rgba(96, 165, 250, 0.12)` | Info badges, fills |

---

## Typography

Two families. Strong scale. Monospace for data.

### Families

- **Sans:** `Inter` — UI, navigation, labels, body prose
- **Mono:** `JetBrains Mono` — URLs, JSON, agent output, code, status codes, IDs

### Scale

| Token | Size | Weight | Line | Usage |
|-------|------|--------|------|-------|
| `text-xl-display` | 1.75rem (28px) | 700 | 1.2 | Page titles (sparse use) |
| `text-lg-heading` | 1.375rem (22px) | 600 | 1.3 | Section headings |
| `text-md-heading` | 1.125rem (18px) | 600 | 1.4 | Card titles, panel headers |
| `text-sm-label` | 0.875rem (14px) | 600 | 1.4 | Labels, badges, nav items |
| `text-sm-body` | 0.875rem (14px) | 400 | 1.5 | Body text, descriptions |
| `text-xs-meta` | 0.75rem (12px) | 500 | 1.4 | Metadata, timestamps, hints |
| `text-xs-mono` | 0.75rem (12px) | 400 | 1.6 | Agent output, JSON, code |

Default body: `text-sm-body` (14px). Dense but readable.

---

## Spacing & Layout

4px base unit.

| Token | Value | Usage |
|-------|-------|-------|
| `space-xs` | 4px | Tight gaps (badge to text, icon to label) |
| `space-sm` | 8px | Default inline gaps, list item padding |
| `space-md` | 12px | Card internal padding, form field gaps |
| `space-lg` | 16px | Section gaps, panel padding |
| `space-xl` | 24px | Region separation |
| `space-2xl` | 32px | Page-level vertical rhythm |

### Radius

| Token | Value | Usage |
|-------|-------|-------|
| `radius-sm` | 4px | Badges, tags, small buttons |
| `radius-md` | 8px | Cards, panels, inputs (default) |
| `radius-lg` | 12px | Modals, large containers |

### Max width

- Content container: `max-w-7xl` (80rem / 1280px)
- Job detail full-bleed logs: `max-w-[1600px]`

---

## Component Patterns

### Panels (the structural unit)

Every region is a panel: `bg-base` surface, `border-subtle` border, `radius-md`. Headers use `text-md-heading` with optional status indicator. No drop shadows — depth comes from surface color contrast and borders.

```
┌─────────────────────────────────┐
│ Panel Header          [status]  │  ← bg-base, border-b border-subtle
├─────────────────────────────────┤
│                                 │
│ Panel body                      │  ← bg-base
│                                 │
└─────────────────────────────────┘
```

### Status badges

Pill-shaped (`rounded-full`), `text-xs-label`, colored bg + text. Always monospace for status values.

```
[● COMPLETED]  [○ IN PROGRESS]  [⚠ AWAITING APPROVAL]  [✕ FAILED]
```

Leading dot/icon indicates state. Color from status tokens.

### Phase timeline (job detail)

Horizontal sequence of phase nodes. Each node: dot + label. Completed = filled success. Active = pulsing accent glow. Pending = muted outline. Failed = danger.

```
●─────●─────●═════○─────○─────○
analyze  map  build  test  clean  done
```

Active phase gets `accent-glow` halo (CSS animation). This is the signature "alive" element.

### Agent log stream

Monospace, `text-xs-mono`, terminal-style. Auto-scroll. Each line: timestamp (muted) + agent label (colored) + message. Alternating subtle row backgrounds for readability. Sticky header with agent filter.

### Data tables

No zebra striping. `border-subtle` row borders. Header row in `bg-raised`. Hover row = `bg-raised`. Dense padding (`space-sm` vertical). Monospace for IDs, URLs, timestamps.

---

## Motion

Restrained. Only where it communicates state.

| Element | Motion | Duration |
|---------|--------|----------|
| Active phase dot | Pulsing glow (opacity + scale) | 2s infinite |
| Live log auto-scroll | Smooth scroll to bottom | 100ms |
| Hover on rows/buttons | Background color transition | 150ms ease |
| Panel appear (JS-rendered) | Subtle fade-in | 200ms |
| Approval banner appear | Slide down from top | 250ms |

No page-load animations. No scroll-triggered reveals. No decorative motion.

---

## Iconography

Inline SVG. 16px default, 20px for nav/headers. Stroke-based (1.5px), matching text color. No icon font. No emoji in UI chrome.

Status icons: filled circle (active), check (success), alert triangle (warning), x-circle (danger), spinner ring (loading).

---

## What NOT to do

- No gradients (background, text, or border)
- No drop shadows on cards/panels (depth via color layering only)
- No glassmorphism / blur effects
- No rounded-2xl on everything (use the radius scale)
- No emoji as UI icons
- No 3-up stat card grids with big numbers
- No skeleton loaders with shimmer animation (use static placeholders)
