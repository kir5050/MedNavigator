/**
 * ResultScreen — HTML-вариант маршрутного листа.
 *
 * Safety divergences vs backend/app/pdf/view_model.py — current state:
 *
 *  - raw `spec.reason` (LLM-generated) is INTENTIONALLY NOT RENDERED.
 *    Under the primary specialist we show the same static safe one-liner
 *    that view_model.py uses (`PRIMARY_ROUTE_ONE_LINER`). Secondary
 *    specialists are name-only — no LLM reasoning surfaces.
 *
 *  - `symptomsSummary` (LLM-generated triage summary) is INTENTIONALLY NOT
 *    USED as the primary "Что вы описали" section. That section is built
 *    from the patient's own chat messages (verbatim). `symptomsSummary`
 *    is used ONLY as a fallback
 *    when the user-message transcript is unavailable (e.g. cold reload
 *    onto /result) — clearly marked and labelled differently in the UI.
 *
 *  - `preparation` checklist is the SHARED STATIC list from
 *    view_model.PREPARATION_CHECKLIST (4 generic items). Per-specialist
 *    `spec.preparation[]` from /triage is LLM-generated (see
 *    backend/app/services/triage_engine.py:413) — validated by
 *    output_safety but not curated KB. We do not render it here.
 *
 *  - Session ID: `sessionId.slice(0, 8)` — same algorithm as
 *    view_model.session_id_short.
 *
 *  - Disclaimer wording still differs (canonical CLAUDE.md §3 string here
 *    vs view_model.DISCLAIMER). Reconcile in a separate PR.
 *
 *  - `QUESTIONS_FOR_DOCTOR` and `URGENT_CARE_BLOCK` from PDF view model
 *    are NOT mirrored on this screen yet.
 *
 *  - Frontend still does not consume a shared route view model from
 *    backend. TODO: expose sanitized route view model from backend to
 *    frontend so PDF and UI cannot drift.
 *
 *  - Emergency PDF branch: `is_crisis_only` PDF remains accessible via
 *    direct URL; CrisisScreen does not surface a PDF CTA. Decision on
 *    that flow — separate PR.
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
  userMessages: string[]
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

/**
 * Static safe one-liner under the primary specialist. Verbatim copy of
 * backend/app/pdf/view_model.PRIMARY_ROUTE_ONE_LINER — reused from the PDF
 * view model on purpose (Brief §3.2).
 */
const PRIMARY_ROUTE_ONE_LINER =
  'Это подходящий первый специалист для очной оценки описанных жалоб и решения, нужен ли другой врач.'

/**
 * Shared preparation checklist (max 5, not per-specialty). Verbatim copy of
 * backend/app/pdf/view_model.PREPARATION_CHECKLIST. Brief §3.5.
 */
const PREPARATION_CHECKLIST: readonly string[] = [
  'вспомните, когда начались жалобы',
  'отметьте, что усиливает или уменьшает симптомы',
  'возьмите список лекарств, витаминов и добавок',
  'возьмите прошлые анализы, снимки или выписки, если они есть',
]

export function ResultScreen({ sessionId, triageData, onRestart }: Props) {
  const [result, setResult] = useState<TriageData>(triageData)
  const [feedbackRating, setFeedbackRating] = useState<number | null>(null)
  const [feedbackHelpful, setFeedbackHelpful] = useState<boolean | null>(null)
  const [feedbackSent, setFeedbackSent] = useState(false)

  useEffect(() => {
    // Cold-load case: /result reached without going through ChatScreen
    // (e.g. browser reload). The /result endpoint does NOT echo back the
    // patient's chat transcript, so userMessages stays as whatever the
    // caller passed (likely []), and we'll fall back to symptomsSummary
    // with a clearly different label.
    getResult(sessionId)
      .then((data) => {
        setResult((prev) => ({
          urgency: data.urgency,
          specialists: data.specialists,
          symptomsSummary: data.symptoms_summary,
          userMessages: prev.userMessages, // preserve what ChatScreen handed us
        }))
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

  // Primary spec gets the safe static one-liner; secondaries — name only.
  const primary = result.specialists[0]
  const secondary = result.specialists.slice(1, 3)

  // "Что вы описали" — built from patient's own chat messages.
  // Fallback to LLM-generated symptomsSummary ONLY when transcript is
  // unavailable (cold reload). UI labels the fallback differently so it
  // is not implied to be the patient's literal words.
  // TODO: expose sanitized `what_patient_described` from backend so this
  // section can consume the same KB-normalised list as the PDF view model.
  const hasUserTranscript = result.userMessages.length > 0

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
              сессия<br /><b>#{sessionId.slice(0, 8)}</b>
            </div>
          </header>

          {(hasUserTranscript || result.symptomsSummary) && (
            <div className="result-row">
              <div className="result-key">
                {hasUserTranscript ? 'Ваши слова' : 'Из диалога'}
              </div>
              <div className="result-val">
                {hasUserTranscript ? (
                  <ul style={{ listStyle: 'disc', paddingLeft: 18, margin: 0 }}>
                    {result.userMessages.map((m, i) => (
                      <li key={i} style={{ marginBottom: i < result.userMessages.length - 1 ? 6 : 0 }}>
                        {m}
                      </li>
                    ))}
                  </ul>
                ) : (
                  result.symptomsSummary
                )}
              </div>
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

          {primary && (
            <div className="result-row">
              <div className="result-key">Специалист</div>
              <div className="result-val">
                <div className="spec">
                  <span className="spec-name">{primary.specialty}</span>
                  <span className="spec-reason">{PRIMARY_ROUTE_ONE_LINER}</span>
                </div>
              </div>
            </div>
          )}

          {secondary.length > 0 && (
            <div className="result-row">
              <div className="result-key">
                Также рассмотрите
              </div>
              <div className="result-val">
                {secondary.map((spec, i) => (
                  <div key={i} className="spec">
                    <span className="spec-name">{spec.specialty}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="result-row">
            <div className="result-key">Подготовка</div>
            <div className="result-val">
              <ul className="prep-list">
                {PREPARATION_CHECKLIST.map((item, i) => (
                  <li key={i}>
                    <input type="checkbox" id={`prep-${i}`} />
                    <label htmlFor={`prep-${i}`}>{item}</label>
                  </li>
                ))}
              </ul>
            </div>
          </div>
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
