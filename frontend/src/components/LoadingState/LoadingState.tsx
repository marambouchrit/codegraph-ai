import { spinner } from '../../ui'

interface Props {
  label: string
}

/** A spinner with a text, announced to screen readers. */
export default function LoadingState({ label }: Props) {
  return (
    <p className="flex items-center gap-2.5 text-muted" role="status">
      <span className={spinner} aria-hidden="true" />
      {label}
    </p>
  )
}
