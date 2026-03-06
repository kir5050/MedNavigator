import { useState, useRef, useEffect } from 'react'
import { sendMessage, uploadFile, runTriage } from '../api/client'
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
      text: 'Здравствуйте! Опишите, что вас беспокоит. Можно прикрепить фото анализов.',
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [ready, setReady] = useState(false)
  const [attachedFile, setAttachedFile] = useState<File | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  async function handleSend() {
    const text = input.trim()
    const file = attachedFile

    // Need at least text or file
    if ((!text && !file) || loading) return

    setInput('')
    setAttachedFile(null)
    setLoading(true)

    // Show user message
    const userText = file
      ? text ? `📎 ${file.name}\n${text}` : `📎 ${file.name}`
      : text
    setMessages((prev) => [...prev, { role: 'user', text: userText }])

    try {
      // 1. Upload file if attached
      if (file) {
        await uploadFile(sessionId, file)
      }

      // 2. Send text message
      const messageText = text || (file ? 'Прикрепил файл с результатами' : '')
      const res = await sendMessage(sessionId, messageText)

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

      if (res.session_status === 'ready') {
        setReady(true)
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'system', text: 'Ошибка. Попробуйте ещё раз.' },
      ])
    } finally {
      setLoading(false)
      inputRef.current?.focus()
    }
  }

  async function handleGetResult() {
    setLoading(true)
    try {
      const result = await runTriage(sessionId)
      onComplete({
        urgency: result.urgency,
        specialists: result.specialists,
        symptomsSummary: result.symptoms_summary,
      })
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'system', text: 'Не удалось получить рекомендацию. Попробуйте ещё раз.' },
      ])
    } finally {
      setLoading(false)
    }
  }

  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    e.target.value = ''
    setAttachedFile(file)
    inputRef.current?.focus()
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

      {ready && !loading && (
        <div className="ready-bar">
          <button className="btn-result" onClick={handleGetResult}>
            Получить рекомендацию
          </button>
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept="image/*,.pdf,.jpg,.jpeg,.png"
        onChange={handleFileSelect}
        style={{ display: 'none' }}
      />

      {attachedFile && (
        <div className="attached-file">
          <span className="attached-file-name">📎 {attachedFile.name}</span>
          <button
            className="attached-file-remove"
            onClick={() => setAttachedFile(null)}
            aria-label="Убрать файл"
          >
            ✕
          </button>
        </div>
      )}

      <div className="input-area">
        <button
          className="attach-btn"
          onClick={() => fileInputRef.current?.click()}
          disabled={loading}
          aria-label="Прикрепить файл"
        >
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none">
            <path d="M17.5 9.58l-7.08 7.08a4.17 4.17 0 01-5.9-5.9l7.08-7.08a2.78 2.78 0 013.93 3.93L8.46 14.7a1.39 1.39 0 01-1.96-1.97l6.49-6.48" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={ready ? 'Можете дополнить или нажмите кнопку выше...' : 'Опишите, что вас беспокоит...'}
          disabled={loading}
          aria-label="Опишите симптомы"
        />
        <button
          className="send-btn"
          onClick={handleSend}
          disabled={(!input.trim() && !attachedFile) || loading}
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
