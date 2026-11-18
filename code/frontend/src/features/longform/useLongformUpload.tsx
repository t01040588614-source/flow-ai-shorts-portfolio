import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { trackEvent } from '../../lib/analytics'
import { FALLBACK_LIMITS, planChunks, type LongformLimits } from './limits'
import { NeedsLoginError, runChunkedUpload, type ChunkedUploadHandle } from './chunkedUpload'
import { formatUploadError } from './formatUploadError'
import {
  deleteLongformUpload,
  fetchLongformLimits,
  getLongformJob,
  importLongformShort,
  startLongformJob,
  type LongformJob,
} from './longformApi'
import {
  clearStoredSession,
  EMPTY_SESSION,
  fileMatchesSession,
  loadStoredSession,
  saveStoredSession,
  type StoredLongformSession,
} from './sessionStore'

const POLL_FAST_MS = 2500
const POLL_SLOW_MS = 6000
const POLL_FAST_FOR_MS = 60_000

type LongformContextValue = {
  session: StoredLongformSession
  limits: LongformLimits
  job: LongformJob | null
  percent: number
  etaLabel: string
  startUpload: (file: File, timelineJobId?: string | null) => Promise<void>
  attachFile: (file: File) => Promise<void>
  pause: () => void
  resume: () => void
  cancel: () => Promise<void>
  importCandidate: (index: number, targetJobId: string) => Promise<{ id: string; name: string; type: string; url: string; duration?: number }>
  clear: () => void
  setJob: (job: LongformJob | null) => void
}

const LongformContext = createContext<LongformContextValue | null>(null)

function etaFrom(startedAt: number, bytesReceived: number, totalSize: number): string {
  if (bytesReceived <= 0 || totalSize <= 0) {
    return '남은 시간 계산 중'
  }
  const elapsed = (Date.now() - startedAt) / 1000
  const rate = bytesReceived / Math.max(elapsed, 0.5)
  const remain = (totalSize - bytesReceived) / rate
  if (!Number.isFinite(remain) || remain < 0) {
    return '남은 시간 계산 중'
  }
  if (remain < 60) {
    return `약 ${Math.max(1, Math.round(remain))}초 남음`
  }
  const minutes = Math.round(remain / 60)
  return `약 ${minutes}분 남음`
}

export function LongformUploadProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<StoredLongformSession>(() => loadStoredSession())
  const [limits, setLimits] = useState<LongformLimits>(FALLBACK_LIMITS)
  const [job, setJob] = useState<LongformJob | null>(null)
  const fileRef = useRef<File | null>(null)
  const handleRef = useRef<ChunkedUploadHandle | null>(null)
  const pausedRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  const sessionRef = useRef(session)
  sessionRef.current = session

  const persist = useCallback((next: StoredLongformSession) => {
    setSession(next)
    if (next.phase === 'idle') {
      clearStoredSession()
      return
    }
    saveStoredSession(next)
  }, [])

  useEffect(() => {
    void fetchLongformLimits().then(setLimits)
    const stored = loadStoredSession()
    if (
      !fileRef.current &&
      stored.uploadId &&
      (stored.phase === 'uploading' || stored.phase === 'paused')
    ) {
      persist({ ...stored, phase: 'need-file' })
    }
  }, [persist])

  useEffect(() => {
    if (!session.processJobId || (session.phase !== 'processing' && session.phase !== 'completed')) {
      return
    }
    let cancelled = false
    const jobId = session.processJobId
    const poll = async () => {
      try {
        const current = await getLongformJob(jobId)
        if (cancelled) {
          return
        }
        setJob(current)
        const latest = sessionRef.current
        if (current.status === 'completed') {
          persist({ ...latest, phase: 'completed', processStatus: current.status, error: null })
          return
        }
        if (current.status === 'failed') {
          persist({
            ...latest,
            phase: 'failed',
            processStatus: current.status,
            error: current.error || '음성 인식·하이라이트 처리에 실패했습니다.',
          })
          return
        }
        persist({ ...latest, processStatus: current.status })
      } catch {
        // Keep polling; a blip should not drop a running server job.
      }
    }
    // Poll fast for the first minute, then slower; skip while the tab is hidden and catch up
    // as soon as it is shown again. One chain at a time (`running`), so no double requests.
    const startedAt = Date.now()
    let timer: number | undefined
    let running = false
    const tick = async () => {
      if (cancelled || running) {
        return
      }
      running = true
      window.clearTimeout(timer)
      if (!document.hidden) {
        await poll()
      }
      running = false
      if (!cancelled) {
        const fast = Date.now() - startedAt < POLL_FAST_FOR_MS
        timer = window.setTimeout(() => void tick(), fast ? POLL_FAST_MS : POLL_SLOW_MS)
      }
    }
    const onVisibility = () => {
      if (!document.hidden) {
        void tick()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    void tick()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [persist, session.phase, session.processJobId])

  const runUpload = useCallback(
    async (file: File, base: StoredLongformSession) => {
      fileRef.current = file
      pausedRef.current = false
      abortRef.current?.abort()
      const abort = new AbortController()
      abortRef.current = abort
      const planned = planChunks(file.size, limits.chunk_bytes)
      trackEvent('longform_upload_start')
      persist({
        ...base,
        filename: file.name,
        totalSize: file.size,
        lastModified: file.lastModified,
        totalChunks: planned.totalChunks,
        chunkSize: planned.chunkSize,
        phase: 'uploading',
        error: null,
        startedAt: base.startedAt || Date.now(),
      })
      const handle = runChunkedUpload(file, {
        chunkBytes: planned.chunkSize,
        uploadId: base.uploadId,
        receivedChunks: base.receivedChunks,
        onProgress: (progress) => {
          persist({
            ...loadStoredSession(),
            uploadId: progress.uploadId,
            bytesReceived: progress.bytesReceived,
            receivedChunks: progress.receivedChunks,
            totalChunks: progress.totalChunks,
            phase: pausedRef.current ? 'paused' : 'uploading',
          })
        },
        signal: abort.signal,
        isPaused: () => pausedRef.current,
      })
      handleRef.current = handle
      try {
        const uploadId = await handle.done
        trackEvent('longform_upload_complete')
        persist({ ...loadStoredSession(), uploadId, phase: 'processing', processStatus: 'queued' })
        const started = await startLongformJob(uploadId)
        persist({
          ...loadStoredSession(),
          uploadId,
          processJobId: started.job_id,
          phase: 'processing',
          processStatus: started.status,
        })
        setJob(started)
      } catch (caught) {
        if (caught instanceof NeedsLoginError) {
          persist({ ...loadStoredSession(), phase: 'needs-login', error: caught.message })
          return
        }
        if (caught instanceof DOMException && caught.name === 'AbortError') {
          return
        }
        persist({ ...loadStoredSession(), phase: 'failed', error: formatUploadError(caught) })
      }
    },
    [limits.chunk_bytes, persist],
  )

  const startUpload = useCallback(
    async (file: File, timelineJobId?: string | null) => {
      await runUpload(file, {
        ...EMPTY_SESSION,
        timelineJobId: timelineJobId ?? null,
        startedAt: Date.now(),
      })
    },
    [runUpload],
  )

  const attachFile = useCallback(
    async (file: File) => {
      const stored = loadStoredSession()
      if (!fileMatchesSession(file, stored)) {
        persist({
          ...stored,
          phase: 'failed',
          error: `같은 파일을 다시 선택해 주세요 (${stored.filename}).`,
        })
        return
      }
      await runUpload(file, stored)
    },
    [persist, runUpload],
  )

  const pause = useCallback(() => {
    pausedRef.current = true
    handleRef.current?.pause()
    persist({ ...loadStoredSession(), phase: 'paused' })
  }, [persist])

  const resume = useCallback(() => {
    pausedRef.current = false
    handleRef.current?.resume()
    persist({ ...loadStoredSession(), phase: 'uploading' })
  }, [persist])

  const cancel = useCallback(async () => {
    handleRef.current?.cancel()
    abortRef.current?.abort()
    const stored = loadStoredSession()
    if (stored.uploadId) {
      try {
        await deleteLongformUpload(stored.uploadId)
      } catch {
        // Session may already be gone.
      }
    }
    fileRef.current = null
    setJob(null)
    persist({ ...EMPTY_SESSION })
  }, [persist])

  const importCandidate = useCallback(async (index: number, targetJobId: string) => {
    if (!session.processJobId) {
      throw new Error('완료된 긴 영상 작업이 없습니다.')
    }
    return importLongformShort(session.processJobId, targetJobId, index)
  }, [session.processJobId])

  const clear = useCallback(() => {
    fileRef.current = null
    setJob(null)
    persist({ ...EMPTY_SESSION })
  }, [persist])

  const percent = session.totalSize > 0 ? Math.min(100, Math.round((session.bytesReceived / session.totalSize) * 100)) : 0
  const etaLabel =
    session.phase === 'uploading' || session.phase === 'paused'
      ? etaFrom(session.startedAt, session.bytesReceived, session.totalSize)
      : ''

  const value = useMemo(
    () => ({
      session,
      limits,
      job,
      percent,
      etaLabel,
      startUpload,
      attachFile,
      pause,
      resume,
      cancel,
      importCandidate,
      clear,
      setJob,
    }),
    [attachFile, cancel, clear, etaLabel, importCandidate, job, limits, pause, percent, resume, session, setJob, startUpload],
  )

  return <LongformContext.Provider value={value}>{children}</LongformContext.Provider>
}

export function useLongformUpload(): LongformContextValue {
  const context = useContext(LongformContext)
  if (!context) {
    throw new Error('useLongformUpload must be used within LongformUploadProvider')
  }
  return context
}
