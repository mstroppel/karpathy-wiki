// Only explicitly classified pre-write rejections permit an input correction.
// Unknown/transport failures must be inspected; never infer that nothing wrote.
export class IngestInputError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
    this.write_state = 'unchanged'
  }
}

export function ingestFailure(error, preparationId = null) {
  return {
    code: error.code ?? 'unknown_state',
    message: error.message ?? String(error),
    write_state: error.write_state ?? 'unknown',
    preparation_id: preparationId,
    correctable: error instanceof IngestInputError,
  }
}
