import { useState } from 'react'
import { WelcomeScreen } from './components/WelcomeScreen'
import { ChatScreen } from './components/ChatScreen'
import { ResultScreen } from './components/ResultScreen'
import type { Specialist } from './api/client'

type Screen = 'welcome' | 'chat' | 'result'

interface TriageData {
  urgency: 'low' | 'medium' | 'high' | 'emergency'
  specialists: Specialist[]
  symptomsSummary: string
}

/**
 * App shell — three top-level views: welcome / chat / result.
 * Crisis is NOT a top-level route: it is a locked sub-state owned by
 * ChatScreen and rendered via <CrisisScreen/>. This keeps the existing
 * flow contract intact while making crisis visually distinct.
 */
export function App() {
  const [screen, setScreen] = useState<Screen>('welcome')
  const [sessionId, setSessionId] = useState<string>('')
  const [triageData, setTriageData] = useState<TriageData | null>(null)

  function handleSessionStart(id: string) {
    setSessionId(id)
    setTriageData(null)
    setScreen('chat')
  }

  function handleTriageComplete(data: TriageData) {
    setTriageData(data)
    setScreen('result')
  }

  function handleRestart() {
    setSessionId('')
    setTriageData(null)
    setScreen('welcome')
  }

  return (
    <div className="app">
      {screen === 'welcome' && <WelcomeScreen onStart={handleSessionStart} />}

      {screen === 'chat' && (
        <ChatScreen
          sessionId={sessionId}
          onComplete={handleTriageComplete}
          onRestart={handleRestart}
        />
      )}

      {screen === 'result' && triageData && (
        <ResultScreen
          sessionId={sessionId}
          triageData={triageData}
          onRestart={handleRestart}
        />
      )}
    </div>
  )
}
