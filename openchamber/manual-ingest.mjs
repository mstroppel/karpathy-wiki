import { randomBytes } from 'node:crypto'

// Mounted after OpenChamber's API authentication gate, before its proxy/queue.
export function manualIngestMiddleware({ token, origin, url, parseBody, fetcher = fetch }) {
  if (!token && !url) return (_req, _res, next) => next()
  if (!/^[a-f0-9]{64}$/.test(token ?? '') || new URL(origin).origin !== origin)
    throw new Error('invalid_manual_ingest_configuration')
  return (req, res, next) => {
    if (req.method === 'GET' && req.path === '/api/wiki-ingest/status')
      return forward(res, '/status')
    const match = /^\/api\/session\/(ses[a-zA-Z0-9_-]{1,100})\/command$/.exec(req.path)
    if (req.method !== 'POST' || !match) return next()
    return parseBody(req, res, () => handle(req, res, next, match))
  }
  async function handle(req, res, next, match) {
    if (req.body?.name !== 'ingest-new') return next()
    // Never accept cross-origin cookie requests or model/backend Basic auth as
    // operator admission. The preceding upstream gate verifies the UI session.
    const resume = req.body.text?.trim() === 'resume'
    if (
      req.headers.origin !== origin ||
      (req.body.text?.trim() && !resume) ||
      req.body.files?.length
    )
      return res.status(403).json({ error: 'explicit_same_origin_ingest_required' })
    try {
      const response = await fetcher(`${url}${resume ? '/resume' : '/ingest-new'}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(
          resume
            ? { confirmed: true }
            : { session_id: match[1], request_id: `req-${randomBytes(16).toString('hex')}` },
        ),
        redirect: 'error',
        signal: AbortSignal.timeout(10000),
      })
      return res.status(response.status).json(await response.json())
    } catch {
      return res.status(503).json({ error: 'manual_ingest_admission_uncertain' })
    }
  }
  async function forward(res, route) {
    try {
      const response = await fetcher(`${url}${route}`, {
        headers: { Authorization: `Bearer ${token}` },
        redirect: 'error',
        signal: AbortSignal.timeout(10000),
      })
      return res.status(response.status).json(await response.json())
    } catch {
      return res.status(503).json({ error: 'manual_ingest_status_unavailable' })
    }
  }
}
