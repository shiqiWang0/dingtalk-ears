# Never (panel v2, Kargul direction; themes)

- No shadows on panels, rows or buttons. Hairlines (#DBDBDB / #EEEEEE) only; the todo dialog is the one overlay with a shadow and it has no border.
- No second accent. Teal #0A7A6E means live/connected and nothing else.
- No orange/peach (#874C2A on #FFE7D2, dot #D9772B) unless Shane has to act: pending draft, unconfirmed send, error, degraded listener.
- No more than one dark pill per row. Secondary actions are ghost text pills; the todo add button is outlined.
- No spinners. Loading is a skeleton of the real layout; on request error the skeleton freezes.
- No helper text or secondary labels that restate what the layout already says. Caveats go in a tooltip.
- No colored badges for counts. Counts sit inline in muted/body text, joined with ' · '.
- No weights other than 500 and 700. No font sizes off the 12/13/14/18/24 scale.
- No motion on layout properties. Only color, opacity, transform; 300ms cubic-bezier(0.76,0,0.24,1); reduced-motion collapses to 1ms.
- No colored backgrounds for handled items. Done is quiet gray (#A3A3A3) with a green check.
- No changes to ids, data hooks, API calls or backend Python.
- No raw colors, font families, radii or shadows outside the theme token blocks (the five swatch colors are the one exception: they depict the themes).
- No theme that changes layout tokens (type scale, spacing, sizes). Themes repaint; they never move things.
- No theme that reuses teal for anything but live, or warm for anything but needs-Shane. Lilac's violet is the primary fill only, never a state.
- No shadow on panels except lilac's faint lift and the overlay shadow each theme defines for the dialog.
- No theme switch animation beyond 300ms color/background/border-color, and none under reduced motion.
- No labels on the switcher. Names live in aria-label/title.
