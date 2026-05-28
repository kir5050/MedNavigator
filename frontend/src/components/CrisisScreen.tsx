import { AppHeader } from './shared/AppHeader'

interface Props {
  /** Текст приходит с бэкенда (emergency_message / message) и показывается дословно. */
  emergencyText: string
  onRestart: () => void
}

/**
 * Найти телефон в строке бэкенда и обернуть в tel:-ссылку.
 * UI не добавляет номеров, которых нет в исходном тексте — это только парсер.
 * Сознательно не матчим короткие короткие коды (типа 103) как литералы,
 * чтобы не было хардкода специальных номеров в коде; такие номера остаются
 * как обычный читаемый текст.
 */
function linkifyPhones(text: string): React.ReactNode[] {
  // Generic Russian phone formats: +7..., 8-..., 8(...)...
  const re = /(\+7[\s\-()\d]{9,}|8[\s\-()][\s\-()\d]{8,})/g
  const parts: React.ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let i = 0
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const raw = m[0]
    const tel = raw.replace(/[\s\-()]/g, '')
    parts.push(
      <a key={`tel-${i++}`} href={`tel:${tel}`}>
        {raw}
      </a>
    )
    last = m.index + raw.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts.length > 0 ? parts : [text]
}

export function CrisisScreen({ emergencyText, onRestart }: Props) {
  return (
    <>
      <AppHeader />
      <main className="crisis" role="alert" aria-live="assertive">
        <div className="crisis-card">
          <div className="crisis-label">Внимание</div>
          <div className="crisis-text">{linkifyPhones(emergencyText)}</div>
          <div className="crisis-foot">
            Диалог приостановлен · обратитесь за очной помощью
          </div>
        </div>

        <button type="button" className="crisis-restart" onClick={onRestart}>
          Начать новый опрос
        </button>
      </main>
    </>
  )
}
