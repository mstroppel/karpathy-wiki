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
    const name = req.body?.name
    const ingest = name === 'ingest-new'
    if (!['ingest-new', 'analysis-save', 'answer-save'].includes(name)) return next()
    // Never accept cross-origin cookie requests or model/backend Basic auth as
    // operator admission. The preceding upstream gate verifies the UI session.
    const raw = typeof req.body.text === 'string' ? req.body.text : ''
    const text = raw.trim()
    const resume = text === 'resume'
    // `ingest-new` keeps its documented trim rule. Authoring saves require
    // literally empty text: whitespace is an argument, not an empty command.
    const badText = ingest ? Boolean(text) && !resume : !(raw === '' || resume)
    const refusal =
      req.headers.origin !== origin || badText || req.body.files?.length
        ? ingest
          ? 'explicit_same_origin_ingest_required'
          : 'explicit_same_origin_authoring_required'
        : null
    if (refusal) return res.status(403).json({ error: refusal })
    // Authoring saves are consumed entirely by this route: there is no fall
    // through to a direct backend write. The model runtime only prepares
    // content as data; the empty command is the explicit save admission and
    // `resume` explicitly verifies an interrupted save.
    const route = ingest
      ? resume
        ? '/resume'
        : '/ingest-new'
      : resume
        ? '/authoring-resume'
        : `/${name}`
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
