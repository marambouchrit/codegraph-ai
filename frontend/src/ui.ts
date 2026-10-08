// Tailwind class lists of the building blocks used on every screen (card, button, input,
// badge, alert), written once here instead of being repeated in each component.

export const page = 'mx-auto grid max-w-[1100px] gap-4 px-4 pt-8 pb-16'

export const card = 'rounded-xl border border-line bg-surface p-6 shadow-card'
export const cardTitle = 'text-lg font-bold'
export const cardSubtitle = 'mt-1 mb-4 text-sm text-muted'
// Small upper-case heading of a sub-section.
export const sectionTitle = 'text-xs font-semibold tracking-wide text-muted uppercase'

const focusRing = 'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent'

// Two utilities for the same property on one element conflict, so each size is its own list.
const inputBase = `rounded-lg border border-line bg-surface text-fg ${focusRing}`
export const input = `${inputBase} w-full px-3 py-2.5`
export const inputSmall = `${inputBase} px-2.5 py-1.5`
// The "choose a file" button inside a file input.
export const fileInput =
  `${inputBase} w-full px-2.5 py-[7px] file:mr-2.5 file:cursor-pointer file:rounded-md file:border ` +
  'file:border-line file:bg-surface-muted file:px-2.5 file:py-0.5 file:text-fg'

const buttonBase =
  'inline-flex cursor-pointer items-center justify-center gap-2 rounded-lg border ' +
  `font-semibold whitespace-nowrap disabled:cursor-not-allowed disabled:opacity-55 ${focusRing}`
const secondary = 'border-line bg-surface text-fg hover:enabled:bg-surface-muted'
export const button = `${buttonBase} px-4 py-2.5 border-transparent bg-accent text-on-accent hover:enabled:bg-accent-hover`
export const buttonSecondary = `${buttonBase} px-4 py-2.5 ${secondary}`
export const buttonSmall = `${buttonBase} px-2.5 py-1.5 ${secondary}`
// A button that looks like a link (jump to another node of the graph).
export const linkButton = 'cursor-pointer text-left wrap-anywhere text-accent hover:underline'

const badgeBase =
  'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[0.8rem] font-semibold whitespace-nowrap'
export const badge = {
  neutral: `${badgeBase} bg-surface-muted text-muted`,
  ok: `${badgeBase} bg-ok-soft text-ok`,
  error: `${badgeBase} bg-danger-soft text-danger`,
  accent: `${badgeBase} bg-accent-soft text-accent`,
}

const alertBase = 'rounded-lg px-3.5 py-2.5 text-sm'
export const alert = {
  neutral: alertBase,
  ok: `${alertBase} bg-ok-soft text-ok`,
  warn: `${alertBase} bg-warn-soft text-warn`,
  error: `${alertBase} bg-danger-soft text-danger`,
}

// "Nothing here yet" box.
export const empty = 'rounded-[10px] border border-dashed border-line p-6 text-center'

export const spinner =
  'inline-block size-4 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent ' +
  'motion-reduce:[animation-duration:2.4s]'

// The small "You" / "CodeGraph AI" line above a message.
export const messageAuthor = 'flex items-center gap-2 text-xs font-bold text-muted'

// Coloured dot of an entity type (legend, node details).
export const typeDot = 'inline-block size-2.5 shrink-0 rounded-full'
// Relationship type shown as coloured code (CALLS, IMPORTS...).
export const relType = 'font-mono text-xs font-bold'
