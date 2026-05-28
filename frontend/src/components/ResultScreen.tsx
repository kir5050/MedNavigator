/**
 * ResultScreen — HTML-вариант маршрутного листа.
 *
 * PDF reconcile (см. backend/app/pdf/view_model.py) — НЕ создаём
 * второй противоречащий формат, но известные расхождения:
 *
 *  - "Жалобы" здесь = `triage.symptoms_summary` (LLM-summary),
 *    в PDF = `what_patient_described` (KB-normalised symptom list).
 *  - "Специалист.reason" здесь рендерится из ответа /triage напрямую;
 *    в PDF этот LLM-текст СОЗНАТЕЛЬНО заменяется на статический
 *    `PRIMARY_ROUTE_ONE_LINER` для безопасности.
 *  - "Подготовка" здесь = `spec.preparation` (per-specialist, из KB);
 *    в PDF используется единый статический `PREPARATION_CHECKLIST` (4 пункта).
 *  - В PDF есть отдельные блоки `QUESTIONS_FOR_DOCTOR` и `URGENT_CARE_BLOCK`,
 *    в этом экране их нет.
 *  - Дисклеймер: здесь — каноническая строка из CLAUDE.md §3 (через `Disclaimer`),
 *    в PDF — собственная редакция (`view_model.DISCLAIMER`). Обе говорят одно и
 *    то же, но текст отличается.
 *
 * TODO (после approval) — выровнять либо frontend под view_model, либо
 * наоборот. В этом PR backend НЕ трогаем; расхождения зафиксированы здесь
 * и в описании PR.
 *
 * Emergency-PDF branch: бэкенд имеет `is_crisis_only` ветку PDF (history +
 * red_flags). В новом crisis-flow PDF CTA скрыт — это сознательно и не
 * регрессия (старый ResultScreen тоже прятал PDF при `urgency === 'emergency'`).
 * Crisis-PDF остаётся доступным напрямую по URL `/api/v1/session/{id}/pdf`,
 * но из UI на него никто не выводит. Решение по этой ветке — отдельным PR.
 */

import { useState, useEffect } from 'react'
import { getResult, getPdfUrl, submitFeedback } from '../api/client'
import type { Specialist } from '../api/client'
import { AppHeader } from './shared/AppHeader'
import { SectionNum } from './shared/SectionNum'
import { Disclaimer } from './shared/Disclaimer'
import { CrisisScreen } from './CrisisScreen'

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

const URGENCY_LABELS: Record<TriageData['urgency'], string> = {
  emergency: 'Экстренно',
  high: 'Срочно',
  medium: 'Планово',
  low: 'Несрочно',
}

const URGENCY_HINT: Record<TriageData['urgency'], string> = {
  emergency: 'Свяжитесь со службой экстренной помощи.',
  high: 'Обратитесь к врачу сегодня.',
  medium: 'Запишитесь в ближайшие дни.',
  low: 'Можно обратиться планово.',
}

function shortSession(id: string): string {
  if (!id) return ''
  return id.replace(/-/g, '').slice(0, 8).toUpperCase()
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
    } catch {
      /* keep silent; user can retry */
    }
  }

  // Backstop: if emergency somehow lands here, render crisis lock instead.
  if (result.urgency === 'emergency') {
    return (
      <CrisisScreen
        emergencyText={result.symptomsSummary || 'Похоже, ситуация требует экстренной помощи.'}
        onRestart={onRestart}
      />
    )
  }

  return (
    <>
      <AppHeader />
      <main className="result">
        <SectionNum>§ 03 — Маршрут</SectionNum>

        <article className="result-doc" aria-label="Маршрутный лист">
          <header className="result-head">
            <div>
              <div className="result-head-label">Маршрутный лист</div>
              <h2>MedNavigator</h2>
            </div>
            <div className="result-session" aria-label="Идентификатор сессии">
              сессия<br /><b>#{shortSession(sessionId)}</b>
            </div>
          </header>

          {result.symptomsSummary && (
            <div className="result-row">
              <div className="result-key">Жалобы</div>
              <div className="result-val">{result.symptomsSummary}</div>
            </div>
          )}

          <div className="result-row">
            <div className="result-key">Срочность</div>
            <div className="result-val">
              <span className={`urgency-pill ${result.urgency}`}>
                {URGENCY_LABELS[result.urgency]}
              </span>
              <div style={{ marginTop: 6, color: 'var(--text-mute)', fontSize: 13 }}>
                {URGENCY_HINT[result.urgency]}
              </div>
            </div>
          </div>

          {result.specialists.length > 0 && (
            <div className="result-row">
              <div className="result-key">
                {result.specialists.length === 1 ? 'Специалист' : 'Специалисты'}
              </div>
              <div className="result-val">
                {result.specialists.map((spec, i) => (
                  <div key={i} className="spec">
                    <span className="spec-name">{spec.specialty}</span>
                    <span className="spec-reason">{spec.reason}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {result.specialists.some((s) => s.preparation && s.preparation.length > 0) && (
            <div className="result-row">
              <div className="result-key">Подготовка</div>
              <div className="result-val">
                {result.specialists.map((spec, i) =>
                  spec.preparation && spec.preparation.length > 0 ? (
                    <div key={i} className="spec" style={i > 0 ? undefined : { paddingTop: 0, borderTop: 'none' }}>
                      {result.specialists.length > 1 && (
                        <span className="spec-reason" style={{ display: 'block', marginBottom: 4 }}>
                          {spec.specialty}
                        </span>
                      )}
                      <ul className="prep-list">
                        {spec.preparation.map((item, j) => (
                          <li key={j}>
                            <input type="checkbox" id={`prep-${i}-${j}`} />
                            <label htmlFor={`prep-${i}-${j}`}>{item}</label>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null
                )}
              </div>
            </div>
          )}
        </article>

        {/* Disclaimer — verbatim canonical string, visible (not buried fine print) */}
        <Disclaimer variant="prominent" />

        {/* CTAs */}
        <div className="result-ctas">
          <a
            href={getPdfUrl(sessionId)}
            className="btn-pdf"
            target="_blank"
            rel="noopener noreferrer"
            download
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="M10 3v10M6 9l4 4 4-4M4 15h12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Скачать маршрутный лист (PDF)
          </a>
          <button type="button" className="btn-restart" onClick={onRestart}>
            Начать новый опрос
          </button>
        </div>

        {/* Feedback */}
        {!feedbackSent ? (
          <section className="feedback-card" aria-label="Обратная связь">
            <h3 className="feedback-title">Оцените полезность маршрута</h3>
            <div className="rating-row" role="radiogroup" aria-label="Оценка от 1 до 5">
              {[1, 2, 3, 4, 5].map((n) => (
                <button
                  key={n}
                  type="button"
                  className={`rating-btn ${feedbackRating === n ? 'active' : ''}`}
                  onClick={() => setFeedbackRating(n)}
                  aria-pressed={feedbackRating === n}
                  aria-label={`Оценка ${n}`}
                >
                  {n}
                </button>
              ))}
            </div>
            <div className="feedback-q">Подходит ли предложенный маршрут?</div>
            <div className="helpful-row" role="radiogroup" aria-label="Подходит ли маршрут">
              {([
                [true, 'Да'],
                [null, 'Частично'],
                [false, 'Нет'],
              ] as const).map(([val, label]) => (
                <button
                  key={label}
                  type="button"
                  className={`helpful-btn ${feedbackHelpful === val ? 'active' : ''}`}
                  onClick={() => setFeedbackHelpful(val)}
                  aria-pressed={feedbackHelpful === val}
                >
                  {label}
                </button>
              ))}
            </div>
            {feedbackRating !== null && (
              <button
                type="button"
                className="cta-primary feedback-submit"
                onClick={handleFeedbackSubmit}
              >
                Отправить
              </button>
            )}
          </section>
        ) : (
          <section className="feedback-card" aria-live="polite">
            <p className="feedback-thanks">Спасибо за обратную связь!</p>
          </section>
        )}
      </main>
    </>
  )
}
