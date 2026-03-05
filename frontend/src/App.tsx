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

export function App() {
  const [screen, setScreen] = useState<Screen>('welcome')
  const [sessionId, setSessionId] = useState<string>('')
  const [triageData, setTriageData] = useState<TriageData | null>(null)

  function handleSessionStart(id: string) {
    setSessionId(id)
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
      <header className="header">
        <h1>MedNavigator</h1>
      </header>

      {screen === 'welcome' && (
        <WelcomeScreen onStart={handleSessionStart} />
      )}

      {screen === 'chat' && (
        <ChatScreen
          sessionId={sessionId}
          onComplete={handleTriageComplete}
          onEmergency={(data) => handleTriageComplete(data)}
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
