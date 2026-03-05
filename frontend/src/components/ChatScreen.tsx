import { useState, useRef, useEffect } from 'react'
import { sendMessage } from '../api/client'
import type { Specialist } from '../api/client'

interface ChatMessage {
  role: 'user' | 'system' | 'emergency'
  text: string
}

interface TriageData {
  urgency: 'low' | 'medium' | 'high' | 'emergency'
  specialists: Specialist[]
  symptomsSummary: string
}

interface Props {
  sessionId: string
  onComplete: (data: TriageData) => void
  onEmergency: (data: TriageData) => void
}

export function ChatScreen({ sessionId, onComplete, onEmergency }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'system',
      text: 'Здравствуйте! Опишите, что вас беспокоит, своими словами.',
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  async function handleSend() {
    const text = input.trim()
    if (!text || loading) return

    setInput('')
    setMessages((prev) => [...prev, { role: 'user', text }])
    setLoading(true)

    try {
      const res = await sendMessage(sessionId, text)

      if (res.is_emergency) {
        setMessages((prev) => [...prev, { role: 'emergency', text: res.message }])
        setTimeout(() => {
          onEmergency({
            urgency: 'emergency',
            specialists: [],
            symptomsSummary: res.message,
          })
        }, 2000)
        return
      }

      setMessages((prev) => [...prev, { role: 'system', text: res.message }])

      if (res.session_status === 'completed') {
        setTimeout(() => {
          onComplete({
            urgency: 'medium',
            specialists: [],
            symptomsSummary: '',
          })
        }, 1500)
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'system', text: 'Произошла ошибка. Попробуйте отправить сообщение снова.' },
      ])
    } finally {
      setLoading(false)
      inputRef.current?.focus()
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="chat">
      <div className="messages">
        {messages.map((msg, i) => (
          <div key={i} className={`message ${msg.role}`}>
            {msg.text}
            {msg.role === 'emergency' && (
              <a href="tel:103">103</a>
            )}
          </div>
        ))}
        {loading && (
          <div className="typing-indicator" aria-label="Система думает">
            <span className="typing-dot" />
            <span className="typing-dot" />
            <span className="typing-dot" />
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="input-area">
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Например: болит голова уже три дня..."
          disabled={loading}
          aria-label="Опишите симптомы"
        />
        <button
          className="send-btn"
          onClick={handleSend}
          disabled={!input.trim() || loading}
          aria-label="Отправить"
        >
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <path d="M3 10l14-7-4 7 4 7L3 10z" fill="currentColor" />
          </svg>
        </button>
      </div>
    </div>
  )
}
