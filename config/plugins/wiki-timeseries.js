import { createConnection } from 'node:net'

const socketPath = '/timeseries-socket/query.sock'

function executeQuery(sql, signal) {
  return new Promise((resolve, reject) => {
    const client = createConnection({ path: socketPath, signal })
    const timer = setTimeout(() => client.destroy(new Error('Query timeout')), 6_000)
    const chunks = []
    let bytes = 0
    client.on('connect', () => client.write(`${JSON.stringify(sql)}\n`))
    client.on('data', (chunk) => {
      bytes += chunk.length
      if (bytes > 15_000) client.destroy(new Error('Result exceeds limit'))
      else chunks.push(chunk)
    })
    client.on('end', () => {
      if (bytes > 0) resolve(Buffer.concat(chunks).toString('utf8'))
      else reject(new Error('No query result'))
    })
    client.on('error', reject)
    client.on('close', () => {
      clearTimeout(timer)
      reject(new Error('Connection closed'))
    })
  })
}

export default {
  id: 'karpathy-wiki.timeseries',
  async setup(ctx) {
    if (
      !process.env.WIKI_TIMESERIES_PROFILES?.split(',').some((part) => part.trim() === 'timeseries')
    )
      return
    await ctx.tool.transform((tools) => {
      tools.add({
        name: 'wiki_timeseries_sql',
        description:
          'Führt ein lesendes SQLite SELECT (auch WITH und Fensterfunktionen) auf dem privaten Zeitreihenarchiv aus. Tabellen: series(series_id, source, entity_id, display_name, description, unit, kind, timestamp_meaning, imported_through_utc, imported_through_us); samples(series_id, timestamp_utc, timestamp_us, value, quality, unit, raw_value). UTC-Zeit in ISO-8601 bzw. Mikrosekunden seit Unix-Epoche. quality=valid kennzeichnet verwendbare Werte; andere Zeilen sind Intervallgrenzen, keine Abdeckung. Ergebnis: columns, rows, truncated oder error.',
        input: {
          type: 'object',
          properties: { sql: { type: 'string', description: 'Eine lesende SQLite-Abfrage' } },
          required: ['sql'],
          additionalProperties: false,
        },
        options: { codemode: false },
        async execute({ sql }, { signal }) {
          if (typeof sql !== 'string' || Buffer.byteLength(sql) > 12_000)
            return { content: '{"error":"SQL size limit."}' }
          try {
            return { content: await executeQuery(sql, signal) }
          } catch {
            signal?.throwIfAborted()
            return { content: '{"error":"Time-series query service unavailable or timed out."}' }
          }
        },
      })
    })
  },
}
