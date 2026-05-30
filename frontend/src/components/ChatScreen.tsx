import { useState, useRef, useEffect } from 'react'
import { sendMessage, runTriage } from '../api/client'
import type { Specialist } from '../api/client'
import { AppHeader } from './shared/AppHeader'
import { SectionNum } from './shared/SectionNum'
import { CrisisScreen } from './CrisisScreen'

interface ChatMessage {
  role: 'user' | 'bot'
  text: string
}

interface TriageData {
  urgency: 'low' | 'medium' | 'high' | 'emergency'
  specialists: Specialist[]
  symptomsSummary: string
  userMessages: string[]
}

interface Props {
  sessionId: string
  onComplete: (data: TriageData) => void
  onRestart: () => void
}

type ProgressStepState = 'idle' | 'active' | 'done'

function role(label: 'АССИСТЕНТ' | 'ВЫ') {
  return label
}

function formatTime(d: Date): string {
  const hh = d.getHours().toString().padStart(2, '0')
  const mm = d.getMinutes().toString().padStart(2, '0')
  return `${hh}:${mm}`
}

export function ChatScreen({ sessionId, onComplete, onRestart }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'bot',
      text: 'Здравствуйте! Опишите, что вас беспокоит.',
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [ready, setReady] = useState(false)
  const [crisisText, setCrisisText] = useState<string | null>(null)
  const docRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Track times alongside messages so they stay stable across renders
  const [times] = useState<Map<number, string>>(() => new Map())

  function getMessageTime(idx: number): string {
    let t = times.get(idx)
    if (!t) {
      t = formatTime(new Date())
      times.set(idx, t)
    }
    return t
  }

  useEffect(() => {
    const el = docRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
  }, [messages, loading])

  // Progress state — derived from chat state
  const userHasSpoken = messages.some((m) => m.role === 'user')
  const step1: ProgressStepState = !userHasSpoken
    ? 'active'
    : ready
    ? 'done'
    : 'done'
  const step2: ProgressStepState = !userHasSpoken
    ? 'idle'
    : ready
    ? 'done'
    : 'active'
  const step3: ProgressStepState = ready ? 'active' : 'idle'

  const progress = [
    { n: '01', label: 'Жалобы',    state: step1 },
    { n: '02', label: 'Уточнение', state: step2 },
    { n: '03', label: 'Маршрут',   state: step3 },
  ]

  async function handleSend() {
    const text = input.trim()

    if (!text || loading || crisisText) return

    setInput('')
    setLoading(true)

    setMessages((prev) => [...prev, { role: 'user', text }])

    try {
      const res = await sendMessage(sessionId, text)

      // Crisis intercept — both is_emergency flag and emergency status are checked.
      if (res.is_emergency || res.session_status === 'emergency') {
        const t = res.emergency_message || res.message
        setCrisisText(t)
        return
      }

      setMessages((prev) => [...prev, { role: 'bot', text: res.message }])

      if (res.session_status === 'ready') {
        setReady(true)
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'bot', text: 'Ошибка. Попробуйте ещё раз.' },
      ])
    } finally {
      setLoading(false)
      inputRef.current?.focus()
    }
  }

  async function handleGetResult() {
    if (loading || crisisText) return
    setLoading(true)
    try {
      const result = await runTriage(sessionId)

      // Crisis intercept on triage path — defense-in-depth.
      //
      // Today the LLM-driven triage prompt
      // (backend/app/prompts/templates.py) is constrained to
      // "low|medium|high", and the /triage response shape
      // (backend/app/main.py — `run_triage`) returns only
      // {urgency, specialists, symptoms_summary, disclaimer} with no
      // `is_emergency` / `session_status` field. Red-flag emergencies are
      // detected pre-LLM on the /message path and never reach /triage.
      //
      // The TS type still admits `urgency: 'emergency'` (api/client.ts
      // TriageResult), so if a future backend change starts emitting that
      // value on the triage path we MUST lock into crisis state instead of
      // rendering an emergency as a regular route document. Cheaper to
      // keep one guard here than to chase a regression later.
      if (result.urgency === 'emergency') {
        setCrisisText(result.symptoms_summary || 'Похоже, ситуация требует экстренной помощи.')
        return
      }

      // Collect literal user-authored text for the result screen.
      const userMessages = messages
        .filter((m) => m.role === 'user')
        .map((m) => m.text.trim())
        .filter((t) => t.length > 0)

      onComplete({
        urgency: result.urgency,
        specialists: result.specialists,
        symptomsSummary: result.symptoms_summary,
        userMessages,
      })
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'bot', text: 'Не удалось получить рекомендацию. Попробуйте ещё раз.' },
      ])
    } finally {
      setLoading(false)
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // Crisis takes over the entire flow — composer locked, no CTA, no auto-timeout.
  if (crisisText) {
    return <CrisisScreen emergencyText={crisisText} onRestart={onRestart} />
  }

  const sectionLabel = userHasSpoken ? '§ 02 — Уточнение' : '§ 01 — Жалобы'

  const canSend = input.trim().length > 0 && !loading

  return (
    <>
      <AppHeader progress={progress} />

      <div className="chat">
        <div className="chat-doc" ref={docRef}>
          <SectionNum>{sectionLabel}</SectionNum>

          {messages.map((msg, i) => (
            <div key={i} className={`chat-row ${msg.role}`}>
              <div className="chat-meta">
                <span className="chat-role">
                  {role(msg.role === 'bot' ? 'АССИСТЕНТ' : 'ВЫ')}
                </span>
                <span className="chat-meta-sep">·</span>
                <span>{getMessageTime(i)}</span>
              </div>
              <div className="chat-text">{msg.text}</div>
            </div>
          ))}

          {loading && (
            <div className="chat-row bot typing" aria-label="Ассистент печатает">
              <div className="chat-meta">
                <span className="chat-role">АССИСТЕНТ</span>
                <span className="chat-meta-sep">·</span>
                <span>сейчас</span>
              </div>
              <div className="typing-dots" aria-hidden="true">
                <i /><i /><i />
              </div>
            </div>
          )}
        </div>

        {ready && !loading && (
          <div className="rec-cta" role="region" aria-label="Готово к рекомендации">
            <div>
              <div className="rec-cta-label">Готово</div>
              <div className="rec-cta-title">Маршрутный лист готов к сборке</div>
            </div>
            <button
              type="button"
              className="rec-cta-btn"
              onClick={handleGetResult}
              disabled={loading}
            >
              Получить рекомендацию
              <span aria-hidden="true">→</span>
            </button>
          </div>
        )}

        <div className="composer">
          <div className="composer-row">
            <input
              ref={inputRef}
              type="text"
              className="composer-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={
                ready
                  ? 'Можете дополнить или нажмите кнопку выше...'
                  : 'Опишите, что вас беспокоит...'
              }
              disabled={loading}
              aria-label="Опишите симптомы"
              autoComplete="off"
              autoCapitalize="sentences"
            />
            <button
              type="button"
              className="composer-send"
              onClick={handleSend}
              disabled={!canSend}
              aria-label="Отправить"
            >
              <svg width="18" height="18" viewBox="0 0 20 20" fill="none">
                <path d="M3 10l14-7-4 7 4 7L3 10z" fill="currentColor" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </>
  )
}
