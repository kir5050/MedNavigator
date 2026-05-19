import { useState, useEffect } from 'react'
import { getResult, getPdfUrl, submitFeedback } from '../api/client'
import type { Specialist } from '../api/client'

interface TriageData {
  urgency: 'low' | 'medium' | 'high' | 'emergency'
  specialists: Specialist[]
  symptomsSummary: string
}

interface Props {
  sessionId: string
  triageData: TriageData
  onRestart: () => void
}

const URGENCY_LABELS: Record<string, string> = {
  emergency: 'ЭКСТРЕННО — вызовите скорую: 103',
  high: 'СРОЧНО — обратитесь к врачу сегодня',
  medium: 'ПЛАНОВО — запишитесь в ближайшие дни',
  low: 'НЕСРОЧНО — можно обратиться планово',
}

export function ResultScreen({ sessionId, triageData, onRestart }: Props) {
  const [result, setResult] = useState<TriageData>(triageData)
  const [feedbackRating, setFeedbackRating] = useState<number | null>(null)
  const [feedbackHelpful, setFeedbackHelpful] = useState<boolean | null>(null)
  const [feedbackSent, setFeedbackSent] = useState(false)

  useEffect(() => {
    getResult(sessionId)
      .then((data) => {
        setResult({
          urgency: data.urgency,
          specialists: data.specialists,
          symptomsSummary: data.symptoms_summary,
        })
      })
      .catch(() => {})
  }, [sessionId])

  async function handleFeedbackSubmit() {
    if (feedbackRating === null) return
    try {
      await submitFeedback(sessionId, feedbackRating, feedbackHelpful)
      setFeedbackSent(true)
    } catch {}
  }

  return (
    <div className="result">
      {/* Urgency badge */}
      <div className={`urgency-badge ${result.urgency}`}>
        {URGENCY_LABELS[result.urgency]}
      </div>

      {/* Emergency call */}
      {result.urgency === 'emergency' && (
        <a href="tel:103" className="emergency-call" aria-label="Позвонить 103">
          103 — Скорая помощь
        </a>
      )}

      {/* Summary */}
      {result.symptomsSummary && (
        <div className="specialist-card">
          <h3>Ваши жалобы</h3>
          <p className="reason">{result.symptomsSummary}</p>
        </div>
      )}

      {/* Specialists */}
      {result.specialists.map((spec, i) => (
        <div key={i} className="specialist-card">
          <h3>{spec.specialty}</h3>
          <p className="reason">{spec.reason}</p>
          {spec.preparation && spec.preparation.length > 0 && (
            <div className="preparation">
              <h4>Подготовка к визиту:</h4>
              <ul className="checklist">
                {spec.preparation.map((item, j) => (
                  <li key={j}>
                    <input type="checkbox" id={`prep-${i}-${j}`} />
                    <label htmlFor={`prep-${i}-${j}`}>{item}</label>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ))}

      {/* PDF Download */}
      {result.urgency !== 'emergency' && (
        <a
          href={getPdfUrl(sessionId)}
          className="pdf-btn"
          target="_blank"
          rel="noopener noreferrer"
          download
        >
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <path d="M10 3v10M6 9l4 4 4-4M4 15h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Скачать маршрутный лист (PDF)
        </a>
      )}

      {/* Feedback */}
      {!feedbackSent ? (
        <div className="feedback-card">
          <h3>Оцените полезность маршрута</h3>
          <div className="rating-row">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                key={n}
                className={`rating-btn ${feedbackRating === n ? 'active' : ''}`}
                onClick={() => setFeedbackRating(n)}
                aria-label={`Оценка ${n}`}
              >
                {n}
              </button>
            ))}
          </div>
          <p style={{ fontSize: 14, marginBottom: 8, color: '#64748B' }}>
            Подходит ли предложенный маршрут?
          </p>
          <div className="helpful-row">
            {([
              [true, 'Да'],
              [null, 'Частично'],
              [false, 'Нет'],
            ] as const).map(([val, label]) => (
              <button
                key={label}
                className={`helpful-btn ${feedbackHelpful === val ? 'active' : ''}`}
                onClick={() => setFeedbackHelpful(val)}
              >
                {label}
              </button>
            ))}
          </div>
          {feedbackRating !== null && (
            <button
              className="btn-primary"
              style={{ width: '100%', marginTop: 12 }}
              onClick={handleFeedbackSubmit}
            >
              Отправить
            </button>
          )}
        </div>
      ) : (
        <div className="feedback-card" style={{ textAlign: 'center' }}>
          <p>Спасибо за обратную связь!</p>
        </div>
      )}

      {/* Disclaimer */}
      <div className="disclaimer">
        MedNavigator не ставит диагноз и не назначает лечение.
        Информация носит справочный характер и помогает подготовиться к визиту.
        Для постановки диагноза и назначения лечения обязательно обратитесь к врачу.
      </div>

      {/* Restart */}
      <button className="btn-back" onClick={onRestart}>
        Начать новый опрос
      </button>
    </div>
  )
}
