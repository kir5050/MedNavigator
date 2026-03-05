import { useState } from 'react'
import { startSession } from '../api/client'

interface Props {
  onStart: (sessionId: string) => void
}

export function WelcomeScreen({ onStart }: Props) {
  const [loading, setLoading] = useState(false)

  async function handleStart() {
    setLoading(true)
    try {
      const data = await startSession()
      onStart(data.session_id)
    } catch {
      alert('Не удалось начать сессию. Попробуйте позже.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="welcome">
      <div className="welcome-icon" aria-hidden="true">
        <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
          <rect width="64" height="64" rx="16" fill="#EFF6FF" />
          <path d="M32 16v32M16 32h32" stroke="#2563EB" strokeWidth="4" strokeLinecap="round" />
        </svg>
      </div>
      <h2>Узнайте, к какому врачу обратиться</h2>
      <p>Опишите симптомы — мы подскажем специалиста и поможем подготовиться к визиту</p>
      <button className="btn-primary" onClick={handleStart} disabled={loading}>
        {loading ? 'Загрузка...' : 'Начать'}
      </button>
      <div className="disclaimer">
        Информация носит справочный характер и не заменяет консультацию врача.
        Сервис не ставит диагнозы и не назначает лечение.
      </div>
    </div>
  )
}
