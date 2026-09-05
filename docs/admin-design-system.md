# AniMemo Admin Design System

## Intent

The administrator workspace extends the public site's Memphis and neo-pop
brutalist identity without turning operational screens into promotional art.
The workspace favors white and light-gray surfaces, compact typography, dense
tables, and predictable controls. Brand color is reserved for selection,
primary actions, state, and key metrics.

## Primitive Tokens

| Group | Tokens | Source |
| --- | --- | --- |
| Brand | `coral`, `pink`, `teal`, `yellow`, `ink`, `cream` | Existing public-site `:root` tokens |
| Neutral | gray 50/100/200/300/500/600/700, white | Added for admin hierarchy and density |
| Spacing | 4, 8, 12, 16, 20, 24, 32px | 4px base rhythm |
| Border | 2px controls, 3px panels | Existing heavy-outline language, reduced for dense UI |
| Motion | 150ms fast, 220ms normal | Operational micro-interactions |

The concrete values live in `src/admin.css` under the primitive-token block.

## Semantic Tokens

| Token | Purpose |
| --- | --- |
| `--admin-color-page` | Main light-gray workspace |
| `--admin-color-surface` | Tables, forms, panels, dialogs |
| `--admin-color-surface-subtle` | Headers, alternate rows, grouped form areas |
| `--admin-color-text` | Primary ink text |
| `--admin-color-text-muted` | Metadata and supporting copy |
| `--admin-color-selection` | Active navigation and warnings |
| `--admin-color-primary` | Primary action |
| `--admin-color-success` | Approved/healthy/ready states |
| `--admin-color-highlight` | Focus and limited attention markers |
| `--admin-color-danger` | Destructive action and failure state |

## Component Rules

| Component | Default | Hover/Focus | Active |
| --- | --- | --- | --- |
| Button | White, 2px ink border, 3px hard shadow | 2px down/right, visible pink focus outline | Shadow collapses |
| Panel | White, 3px ink border, 6px hard shadow | No lift | N/A |
| Input | White, 2px ink border, 38px minimum height | Pale-yellow fill and selection ring | Stable geometry |
| Navigation | Dark rail, muted labels | White text and subtle surface | Yellow selected item with teal hard shadow |
| Status | Full brand fill for compact chips; pale tints only for larger notices | No motion | N/A |
| Data row | White, 1px divider | Pale-yellow row highlight | Stable geometry |
| Dialog | White, 3px ink border, semantic hard shadow | GSAP scale/fade entrance | Escape and explicit close supported |

## Motion

- CSS handles ordinary hover, focus, press, and selection in 150-220ms.
- GSAP is limited to page entrance, module changes, settings-panel changes,
  dialogs, and success feedback.
- `prefers-reduced-motion: reduce` disables animation and transforms while
  retaining immediate state changes.

## Responsive Behavior

- Desktop uses a 236px navigation rail and four-column metric strip.
- Medium screens reduce the rail and collapse metrics and operational cards.
- Below 900px the rail becomes a compact native module selector.
- Below 680px resource rows and audit records become stacked operational
  cards, forms become single-column, and dialogs use the viewport width.

## Accessibility

- Interactive controls retain visible focus rings and semantic HTML controls.
- Operational controls use 12px or larger text, supporting metadata uses 11px
  or larger text, and only short uppercase kickers may use 10px text.
- Status is always expressed with text, not color alone.
- Operational tables use crisp white rows, dark metadata, black separators,
  and full-strength brand color on compact avatars and status chips.
- The user directory shows account state and staff role as separate compact
  chips so the color remains semantic instead of becoming a vague pastel tint.
- Resource filters have explicit accessible labels.
- Dialogs receive focus on open, close with Escape, and use dialog roles.
- Dense desktop controls remain at least 38px high; primary mobile controls are
  expanded to 40px or more.
