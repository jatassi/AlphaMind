// Reference-ID utilities (non-component exports separated per react-refresh rule).
// ALP-674.

// Reference-ID prefix vocabulary mirrors synthesizer.md.
export const REF_PREFIXES = ['SA-TECH', 'SA-FIN', 'SA-ENERGY', 'CR', 'QR', 'AR'] as const

export type RefPrefix = (typeof REF_PREFIXES)[number]

export function isRefPrefix(s: string): s is RefPrefix {
  return (REF_PREFIXES as readonly string[]).includes(s)
}

export function extractRefPrefix(refId: string): RefPrefix | null {
  for (const prefix of REF_PREFIXES) {
    if (refId.startsWith(`${prefix}-`)) {
      return prefix
    }
  }
  return null
}

// Regex for reference-ID patterns embedded in narrative text.
export const REF_ID_PATTERN = /\[(SA-TECH|SA-FIN|SA-ENERGY|CR|QR|AR)-\d+\]/g
