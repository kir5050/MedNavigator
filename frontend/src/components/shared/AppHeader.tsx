interface ProgressStep {
  n: string
  label: string
  state: 'idle' | 'active' | 'done'
}

interface Props {
  progress?: ProgressStep[]
}

/**
 * Sticky app header with the MedNavigator logo and (optional) progress strip.
 * Progress strip is an INDICATOR, not tabs — cells are not clickable.
 */
export function AppHeader({ progress }: Props) {
  return (
    <header className="app-header">
      <div className="app-header-row">
        <div className="app-logo">
          <b>MedNavigator</b>
        </div>
      </div>
      {progress && progress.length > 0 && (
        <div className="progress-row" role="progressbar" aria-label="Шаги опроса">
          {progress.map((step) => (
            <div key={step.n} className={`progress-cell ${step.state}`} aria-current={step.state === 'active' ? 'step' : undefined}>
              <span className="pn">{step.n}</span>
              {step.label}
            </div>
          ))}
        </div>
      )}
    </header>
  )
}
