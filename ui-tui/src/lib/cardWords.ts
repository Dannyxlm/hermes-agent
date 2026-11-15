// A Paper card's words from its `show_card` receipt, only when the card was accepted (`ok: true`);
// a refused call shows nothing (contract cards-v1, README § Receipt). The receipt arrives parsed,
// or as its JSON text on paths that forward the raw tool result.
const parsed = (result: unknown): unknown => {
  if (typeof result !== 'string') {
    return result
  }

  try {
    return JSON.parse(result)
  } catch {
    return null
  }
}

export const acceptedCardAlt = (result: unknown): null | string => {
  const receipt = parsed(result)

  if (!receipt || typeof receipt !== 'object') {
    return null
  }

  const { alt, ok } = receipt as { alt?: unknown; ok?: unknown }

  return ok === true && typeof alt === 'string' && alt.trim() ? alt : null
}
