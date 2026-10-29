# Panel v2 spec (Kargul style)

**Shell.** Gray page #EFEFEF, one white app panel (12px radius, 1px #DBDBDB hairline, max 1280px). Sticky top bar: brand left, status pill right (gray pulsing dot while starting, teal tint when live, peach when degraded). Below: main list + fixed 320px right rail split by a hairline; the rail is sticky and scrolls on its own.

**Type.** Geist (installed font; CSP blocks shipping files) → -apple-system / PingFang SC / Noto Sans CJK. Weights 500 body, 700 headings. Scale 24 (h1, -0.48 tracking) / 18 (h2) / 14 body / 13 / 12 meta. Times use Geist Mono.

**Color.** Ink #232323, body #707070, muted #9D9D9D (separators, placeholders, disabled), done #A3A3A3. Teal #0A7A6E only for live. Brown #874C2A + peach #FFE7D2 only for needs-Shane.

**List head.** h1 待处理, inline count "N 待审" (brown when N>0), segmented control right-aligned on the title baseline (active = dark pill). At ≤640px the control goes full width under the title.

**Rows.** Two-column grid: 88px mono time axis + content (max 720). Name 700 + sources as inline text joined by ' · '. Rows that need Shane get an orange dot hanging left of the name, so names stay aligned. Codex output sits in a tinted 12px block: summary, white textarea, state label, actions. Ghost actions align with the summary text; the single dark pill (确认发送 / 生成草稿) sits right.

**States.** Draft labels: none = dashed dot, sending = teal pulse, unknown = brown, stale = gray. Buttons: ghost hover = #F2F2F2 fill, active = scale 0.97, focus-visible = 2px ink outline offset 2, disabled = 40% opacity (primary: gray fill), busy = aria-busy opacity pulse. Empty = one quiet line in the content column. Error = peach strip under the title; skeleton freezes.

**Rail.** 总结 (refresh icon button, summary, meta line), 待办 (inline count, rows; done = gray + green check, unknown = brown, creating = teal), 实时接收 (two-column key-value: 单聊 / @我 / groups → "N 条", 更新 → HH:MM:SS mono; listener caveat in tooltip).

**Archive.** "已处理 N" disclosure with rotating chevron; handled rows in gray.

**Motion.** 300ms cubic-bezier(0.76,0,0.24,1) on color/opacity/transform only; row enter fade+4px rise; dialog scale 0.97→1. prefers-reduced-motion: 1ms, no press scale.

**Responsive.** ≤900px: single column, order bar / 总结 / list / 待办 / 实时接收. ≤640px: 16px padding, time moves right of the name.

## Themes

Five themes share one structure: type scale, spacing, control heights, the 88px time axis and the 320px rail are on `:root` and never change, so switching only repaints. `:root` alone is kargul; `:root[data-theme='…']` overrides color, font family, radius, hairline, shadow and focus tokens only.

**Switcher.** Five 12px swatches (24px targets) right of the status pill, `role="group"` `aria-label="主题"`, each button `aria-pressed` with a one-word `aria-label`/`title` (浅色 / 深色 / 纸本 / 等宽 / 淡紫). Swatch = page/primary split, shape follows the theme's radius (round, square-ish, square). Unselected 70% opacity, hover 100%, selected gets a 1px gap + 2.5px ink ring, active scale 0.9, focus-visible = the shared 2px focus outline. Choice persists in `localStorage['dte-panel-theme']`; a nonce'd inline script in `<head>` applies it before first paint (CSP allows `script-src 'nonce-…'`; unknown values fall back to kargul). Switching adds `.theme-shift` to `<html>` for 400ms: 300ms transition on color, background-color, border-color only; skipped under prefers-reduced-motion.

| Token | kargul | dark | paper | mono | lilac |
|---|---|---|---|---|---|
| `--page` | #EFEFEF | #1A1A1A | #E9E2D4 | #FFFFFF | #E4DFF6 |
| `--surface` | #FFFFFF | #232323 | #FBF8F1 | #FFFFFF | #FFFFFF |
| `--tint` (Codex block) | #F7F7F7 | #2E2E2E | #F3EDE1 | #F4F4F4 | #F3F0FC |
| `--hover` / `--hover-on-tint` | #F2F2F2 / #FFF | #313131 / #3A3A3A | #F1EADC / #FBF8F1 | #EBEBEB / #FFF | #F0EDFA / #FFF |
| `--line` / `--row-line` | #DBDBDB / #EEE | white 12% / 7% | #DCD2BF / #EAE2D3 | #0A0A0A / #D4D4D4 | #E0DCF0 / #EFECF8 |
| `--app-line` | #DBDBDB | white 15% | #CFC3AD | #0A0A0A | transparent |
| `--ink` | #232323 | #EDEDED | #2B241B | #0A0A0A | #26223A |
| `--body` | #707070 | #A6A6A6 | #685D4D | #3D3D3D | #625C7C |
| `--muted` | #9D9D9D | #6E6E6E | #A69A86 | #8A8A8A | #A19BBA |
| `--done` (handled) | #A3A3A3 | #7C7C7C | #958A78 | #858585 | #8F8AA6 |
| `--check` | #1CB16F | #3DC98A | #3D8A57 | #0B8A4F | #2BA769 |
| `--accent` / `--accent-tint` (live only) | #0A7A6E / #F2F8F7 | #4FD1BF / 12% | #1D6B5F / #E2ECE5 | #006B5F / #DFF1ED | #0B7469 / #E2F4F1 |
| `--warn` / `--warn-ink` / `--warn-tint` (needs Shane only) | #D9772B / #874C2A / #FFE7D2 | #F29A52 / #F6B88A / 15% | #C55F27 / #8B3912 / #F5DCC5 | #E0580C / #9A3300 / #FFE2CC | #E07836 / #8F4216 / #FFE4D3 |
| `--primary` / `--on-primary` | #232323 / #FFF | #EDEDED / #1A1A1A | #2B241B / #FBF8F1 | #0A0A0A / #FFF | #4B3FA8 / #FFF |
| `--font-head` | Geist stack | Geist stack | Iowan / Songti SC / Noto Serif CJK | JetBrains/IBM Plex Mono (all text) | Geist stack |
| `--radius-app` / `-block` / `-control` | 12 / 12 / pill | 12 / 12 / pill | 4 / 4 / 4 | 0 / 0 / 0 | 24 / 16 / pill |
| `--shadow-app` / overlay | none / soft | none / deep + 15% ring | none / warm soft | none / 6px hard offset | faint lift / soft violet |

Measured contrast (WCAG, on surface unless noted), body text pairs all ≥ 4.5:1:

| Pair | kargul | dark | paper | mono | lilac |
|---|---|---|---|---|---|
| ink | 15.7 | 13.4 | 14.4 | 19.8 | 15.3 |
| body | 4.95 | 6.46 | 6.07 | 10.9 | 6.29 |
| body on tint | 4.62 | 5.58 | 5.52 | 9.88 | 5.59 |
| accent / live pill | 5.22 / 4.86 | 8.39 / 6.56 | 5.96 / 5.23 | 6.43 / 5.49 | 5.65 / 4.96 |
| warn text / strip | 6.78 / 5.69 | 9.09 / 6.87 | 7.36 / 5.92 | 7.37 / 5.97 | 7.10 / 5.84 |
| primary label | 15.7 | 14.9 | 14.4 | 19.8 | 8.15 |
| handled (quiet on purpose) | 2.52 | 3.77 | 3.20 | 3.69 | 3.31 |
