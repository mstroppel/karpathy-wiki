// Outbound runtime access only. No runtime route can call the trusted writer.
export function manualRuntime({ url, password, fetcher = fetch }) {
  const request = async (route, body) => {
    const response = await fetcher(`${url}${route}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        Authorization: `Basic ${Buffer.from(`opencode:${password}`).toString('base64')}`,
        'Content-Type': 'application/json',
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(120000),
      redirect: 'error',
    })
    if (!response.ok) throw new Error('runtime_request_failed')
    return (await response.json()).data
  }
  return {
    session: (id) => request(`/api/session/${id}`),
    create: (parentID, model) =>
      request('/api/session', {
        parentID,
        model,
        agent: 'build',
        title: 'Private manual ingest worker',
        permissions: [{ action: '*', resource: '*', effect: 'deny' }],
      }),
    generate: async (id, prompt) => (await request(`/api/session/${id}/generate`, { prompt })).text,
    note: (id, text, resume = false) =>
      request(`/api/session/${id}/synthetic`, {
        text,
        description: 'Verified manual ingest status',
        resume,
      }),
  }
}
