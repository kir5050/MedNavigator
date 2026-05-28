import { useState } from 'react'
import { startSession } from '../api/client'
import { AppHeader } from './shared/AppHeader'
import { SectionNum } from './shared/SectionNum'
import { Disclaimer } from './shared/Disclaimer'

interface Props {
  onStart: (sessionId: string) => void
}

export function WelcomeScreen({ onStart }: Props) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleStart() {
    setLoading(true)
    setError(null)
    try {
      const data = await startSession()
      onStart(data.session_id)
    } catch {
      setError('Не удалось начать сессию. Попробуйте позже.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <AppHeader />
      <main className="welcome">
        <SectionNum>§ 01 — Приёмная</SectionNum>

        <h1 className="welcome-hl">
          Узнайте, к какому <span className="accent">врачу</span> обратиться
        </h1>

        <p className="welcome-sub">
          Опишите симптомы — мы подскажем специалиста и поможем подготовиться к визиту.
        </p>

        <ol className="steps3" aria-label="Как это работает">
          <li className="step3">
            <span className="step3-n">01</span>
            <span>
              <span className="step3-t">Опишите жалобу</span>
              <span className="step3-d">словами, как удобно</span>
            </span>
          </li>
          <li className="step3">
            <span className="step3-n">02</span>
            <span>
              <span className="step3-t">Уточняем детали</span>
              <span className="step3-d">несколько коротких вопросов</span>
            </span>
          </li>
          <li className="step3">
            <span className="step3-n">03</span>
            <span>
              <span className="step3-t">Маршрутный лист</span>
              <span className="step3-d">специалист и подготовка к визиту</span>
            </span>
          </li>
        </ol>

        <button
          type="button"
          className="cta-primary"
          onClick={handleStart}
          disabled={loading}
        >
          {loading ? 'Загрузка...' : 'Начать'}
          {!loading && <span className="arrow" aria-hidden="true">→</span>}
        </button>

        {error && (
          <p
            role="alert"
            style={{
              marginTop: 12,
              fontSize: 13,
              color: 'var(--copper-2)',
              fontFamily: 'var(--font-sans)',
            }}
          >
            {error}
          </p>
        )}

        <Disclaimer />
      </main>
    </>
  )
}
