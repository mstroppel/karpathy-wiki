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
    if (!['ingest-new', 'analysis-save', 'answer-save'].includes(req.body?.name)) return next()
    // Never accept cross-origin cookie requests or model/backend Basic auth as
    // operator admission. The preceding upstream gate verifies the UI session.
    const text = req.body.text?.trim() ?? ''
    const resume = text === 'resume'
    const refusal =
      req.headers.origin !== origin || (text && !resume) || req.body.files?.length
        ? 'explicit_same_origin_ingest_required'
        : null
    if (refusal && req.body.name === 'ingest-new') return res.status(403).json({ error: refusal })
    if (refusal) return res.status(403).json({ error: 'explicit_same_origin_authoring_required' })
    // Authoring saves are consumed entirely by this route: there is no fall
    // through to a direct backend write. The model runtime only prepares
    // content as data; the empty command is the explicit save admission and
    // `resume` explicitly verifies an interrupted save.
    const route =
      req.body.name === 'ingest-new'
        ? resume
          ? '/resume'
          : '/ingest-new'
        : resume
          ? '/authoring-resume'
          : `/${req.body.name}`
    try {
      const response = await fetcher(`${url}${route}`, {
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
