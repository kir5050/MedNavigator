const API_BASE = import.meta.env.VITE_API_URL || ''

export interface SessionStartResponse {
  session_id: string
  message: string
  disclaimer: string
}

export interface MessageResponse {
  message: string
  session_status: 'collecting' | 'ready' | 'triaging' | 'completed' | 'emergency'
  disclaimer: string
  extracted_symptoms: string[]
  is_emergency: boolean
  emergency_message: string | null
}

export interface TriageResult {
  urgency: 'low' | 'medium' | 'high' | 'emergency'
  specialists: Specialist[]
  symptoms_summary: string
  preparation: string
  disclaimer: string
}

export interface Specialist {
  specialty: string
  reason: string
  priority: number
  preparation?: string[]
}

export async function startSession(): Promise<SessionStartResponse> {
  const res = await fetch(`${API_BASE}/api/v1/session/start`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to start session')
  return res.json()
}

export async function sendMessage(sessionId: string, text: string): Promise<MessageResponse> {
  const res = await fetch(`${API_BASE}/api/v1/session/${sessionId}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!res.ok) throw new Error('Failed to send message')
  return res.json()
}

export async function runTriage(sessionId: string): Promise<TriageResult> {
  const res = await fetch(`${API_BASE}/api/v1/session/${sessionId}/triage`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to run triage')
  return res.json()
}

export async function getResult(sessionId: string): Promise<TriageResult> {
  const res = await fetch(`${API_BASE}/api/v1/session/${sessionId}/result`)
  if (!res.ok) throw new Error('Failed to get result')
  return res.json()
}

export function getPdfUrl(sessionId: string): string {
  return `${API_BASE}/api/v1/session/${sessionId}/pdf`
}

export interface UploadResponse {
  status: string
  filename: string
  analysis: string
}

export async function uploadFile(sessionId: string, file: File): Promise<UploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  const res = await fetch(`${API_BASE}/api/v1/session/${sessionId}/upload`, {
    method: 'POST',
    body: formData,
  })
  if (!res.ok) throw new Error('Failed to upload file')
  return res.json()
}

export async function submitFeedback(
  sessionId: string,
  rating: number,
  wasHelpful: boolean | null,
  comment?: string
): Promise<void> {
  await fetch(`${API_BASE}/api/v1/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      rating,
      was_helpful: wasHelpful,
      comment: comment || null,
    }),
  })
}
