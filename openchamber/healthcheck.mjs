try {
  const response = await fetch('http://127.0.0.1:3000/health', {
    signal: AbortSignal.timeout(5000),
  })
  const health = await response.json()
  if (!response.ok || health.status !== 'ok' || health.isOpenCodeReady !== true) process.exit(1)
} catch {
  process.exit(1)
}
