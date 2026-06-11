import { useEffect, useRef, useState } from 'react'
import { transcribeAudio, TranscribeError } from '../api/client'

export const VOICE_INPUT_ENABLED = import.meta.env.VITE_VOICE_INPUT_ENABLED === 'true'

const MAX_RECORDING_MS = 180_000
const MIN_RECORDING_MS = 2_000
const MAX_AUDIO_BYTES = 15 * 1024 * 1024

// iOS Safari cannot record webm — audio/mp4 is the required fallback.
const MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']

// Approved UI copy — inserted verbatim, do not rephrase.
const COPY = {
  micLabel: 'Надиктовать голосом',
  recordingHint: 'Говорите… Запись остановится автоматически через 3 минуты',
  autoStopNotice: 'Запись остановлена: достигнут лимит 3 минуты',
  transcribing: 'Распознаём речь…',
  reviewCaption: 'Проверьте текст перед отправкой — при необходимости исправьте',
  tooShort: 'Запись слишком короткая. Попробуйте ещё раз или введите текст вручную.',
  emptyTranscript: 'Не удалось распознать речь. Попробуйте ещё раз или введите текст вручную.',
  micDenied: 'Нет доступа к микрофону. Разрешите доступ в настройках браузера или введите текст вручную.',
  requestFailed: 'Не получилось обработать запись. Попробуйте ещё раз или введите текст вручную.',
  retry: 'Повторить',
} as const

type Phase = 'idle' | 'recording' | 'transcribing'

interface Props {
  disabled: boolean
  /** True when the composer field is empty — hides the stale review caption. */
  inputEmpty: boolean
  onTranscript: (text: string) => void
  /** Called after phase transitions that resize the controls cluster. */
  onLayoutChange?: () => void
}

function isSupported(): boolean {
  return (
    typeof MediaRecorder !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia
  )
}

function formatTimer(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  const mm = String(Math.floor(totalSec / 60)).padStart(2, '0')
  const ss = String(totalSec % 60).padStart(2, '0')
  return `${mm}:${ss}`
}

export function VoiceInput({ disabled, inputEmpty, onTranscript, onLayoutChange }: Props) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [elapsedMs, setElapsedMs] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const [errorText, setErrorText] = useState<string | null>(null)
  const [retryBlob, setRetryBlob] = useState<Blob | null>(null)

  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const pickedMimeRef = useRef('')
  const startedAtRef = useRef(0)
  const autoStoppedRef = useRef(false)
  const tickTimerRef = useRef<number | null>(null)
  const autoStopTimerRef = useRef<number | null>(null)
  const mountedRef = useRef(true)
  // Synchronous re-entry guard: two rapid mic taps both pass the phase
  // check before React applies state, which would leak a live MediaStream.
  const startingRef = useRef(false)

  // Runs after the DOM for the new phase is committed, so the parent
  // measures the final layout.
  useEffect(() => {
    onLayoutChange?.()
  }, [phase])

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      // Stop silently on unmount: drop handlers first so onstop does not
      // fire a transcription for a screen that no longer exists.
      const recorder = recorderRef.current
      if (recorder) {
        recorder.ondataavailable = null
        recorder.onstop = null
        if (recorder.state !== 'inactive') recorder.stop()
        recorderRef.current = null
      }
      releaseStream()
      clearTimers()
    }
  }, [])

  function clearTimers() {
    if (tickTimerRef.current !== null) {
      window.clearInterval(tickTimerRef.current)
      tickTimerRef.current = null
    }
    if (autoStopTimerRef.current !== null) {
      window.clearTimeout(autoStopTimerRef.current)
      autoStopTimerRef.current = null
    }
  }

  function releaseStream() {
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
  }

  async function startRecording() {
    if (startingRef.current || phase !== 'idle' || disabled) return
    startingRef.current = true
    try {
      setNotice(null)
      setErrorText(null)
      setRetryBlob(null)

      let stream: MediaStream
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      } catch {
        setErrorText(COPY.micDenied)
        return
      }
      if (!mountedRef.current || recorderRef.current) {
        stream.getTracks().forEach((t) => t.stop())
        return
      }

      const mimeType = MIME_CANDIDATES.find((t) => MediaRecorder.isTypeSupported(t))
      let recorder: MediaRecorder
      try {
        recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream)
      } catch {
        stream.getTracks().forEach((t) => t.stop())
        setErrorText(COPY.requestFailed)
        return
      }

      pickedMimeRef.current = mimeType ?? ''
      chunksRef.current = []
      recorder.ondataavailable = (e: BlobEvent) => {
        if (e.data.size > 0) chunksRef.current.push(e.data)
      }
      recorder.onstop = handleRecorderStop
      recorderRef.current = recorder
      streamRef.current = stream
      startedAtRef.current = Date.now()
      autoStoppedRef.current = false
      setElapsedMs(0)
      recorder.start()
      setPhase('recording')
      tickTimerRef.current = window.setInterval(() => {
        setElapsedMs(Date.now() - startedAtRef.current)
      }, 250)
      autoStopTimerRef.current = window.setTimeout(() => {
        autoStoppedRef.current = true
        stopRecording()
      }, MAX_RECORDING_MS)
    } finally {
      startingRef.current = false
    }
  }

  function stopRecording() {
    const recorder = recorderRef.current
    if (!recorder || recorder.state === 'inactive') return
    // Kill the pending autostop right away so it cannot fire between a
    // manual stop and the async onstop, mislabeling the stop as automatic.
    clearTimers()
    recorder.stop() // final dataavailable + onstop fire asynchronously
  }

  function handleRecorderStop() {
    clearTimers()
    releaseStream()
    const recorder = recorderRef.current
    recorderRef.current = null
    if (!mountedRef.current) return

    const durationMs = Date.now() - startedAtRef.current
    const blobType = recorder?.mimeType || pickedMimeRef.current || 'audio/webm'
    const blob = new Blob(chunksRef.current, { type: blobType })
    chunksRef.current = []

    if (durationMs < MIN_RECORDING_MS) {
      // Too short to be a real complaint — never uploaded.
      setPhase('idle')
      setNotice(COPY.tooShort)
      return
    }
    if (autoStoppedRef.current) setNotice(COPY.autoStopNotice)
    void submit(blob)
  }

  async function submit(blob: Blob) {
    // Size guard before upload; retrying the same blob cannot succeed,
    // so no retry button for this case.
    if (blob.size > MAX_AUDIO_BYTES) {
      setPhase('idle')
      setRetryBlob(null)
      setErrorText(COPY.requestFailed)
      return
    }
    setPhase('transcribing')
    setErrorText(null)
    setRetryBlob(blob)
    try {
      const res = await transcribeAudio(blob)
      if (!mountedRef.current) return
      setPhase('idle')
      setRetryBlob(null)
      setNotice(COPY.reviewCaption)
      onTranscript(res.text)
    } catch (err) {
      if (!mountedRef.current) return
      setPhase('idle')
      setNotice(null)
      if (err instanceof TranscribeError && err.kind === 'empty_transcript') {
        // Re-sending the same audio would fail the same way — no retry.
        setRetryBlob(null)
        setErrorText(COPY.emptyTranscript)
      } else {
        // Network / 5xx / 429: keep the blob so retry re-sends it as is.
        setErrorText(COPY.requestFailed)
      }
    }
  }

  if (!isSupported()) return null

  const statusLines: string[] = []
  if (phase === 'recording') statusLines.push(COPY.recordingHint)
  if (phase === 'transcribing') {
    statusLines.push(COPY.transcribing)
    if (notice) statusLines.push(notice)
  }
  // The review caption is only meaningful while the field still has text.
  const staleCaption = notice === COPY.reviewCaption && inputEmpty
  if (phase === 'idle' && notice && !errorText && !staleCaption) statusLines.push(notice)
  const showError = phase === 'idle' && errorText !== null

  return (
    <>
      <div className="voice-controls">
        {phase === 'recording' ? (
          <button type="button" className="voice-stop" onClick={stopRecording}>
            <svg width="14" height="14" viewBox="0 0 20 20" aria-hidden="true">
              <rect x="4" y="4" width="12" height="12" rx="2.5" fill="currentColor" />
            </svg>
            <span className="voice-timer">{formatTimer(elapsedMs)}</span>
          </button>
        ) : (
          <button
            type="button"
            className="voice-mic"
            onClick={() => void startRecording()}
            disabled={disabled || phase === 'transcribing'}
            aria-label={COPY.micLabel}
            title={COPY.micLabel}
          >
            <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <rect
                x="7.25" y="2.25" width="5.5" height="9.5" rx="2.75"
                stroke="currentColor" strokeWidth="1.5"
              />
              <path
                d="M4.75 9.75a5.25 5.25 0 0 0 10.5 0"
                stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
              />
              <path
                d="M10 15v2.75"
                stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
              />
            </svg>
          </button>
        )}
      </div>
      {(statusLines.length > 0 || showError) && (
        <div className={`voice-status${showError ? ' error' : ''}`} role="status">
          {statusLines.map((line) => (
            <span key={line}>{line}</span>
          ))}
          {showError && (
            <span className="voice-error-row">
              {errorText}
              {retryBlob && (
                <button
                  type="button"
                  className="voice-retry"
                  onClick={() => void submit(retryBlob)}
                >
                  {COPY.retry}
                </button>
              )}
            </span>
          )}
        </div>
      )}
    </>
  )
}
