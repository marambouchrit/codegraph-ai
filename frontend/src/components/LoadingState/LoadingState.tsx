interface Props {
  label: string
}

/** A spinner with a text, announced to screen readers. */
export default function LoadingState({ label }: Props) {
  return (
    <p className="muted" role="status" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span className="spinner" aria-hidden="true" />
      {label}
    </p>
  )
}
