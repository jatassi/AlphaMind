// yaml-dump — thin serialization shim for config editor pages (ALP-682).
//
// JSON is a strict subset of YAML 1.2; the backend's ``yaml.safe_load``
// accepts JSON-format payloads transparently. Using JSON.stringify avoids
// pulling in a YAML-serialization library while keeping the round-trip
// (frontend form state → YAML text → backend yaml.safe_load) correct.
//
// The output uses 2-space indentation for readability in the network
// inspector; the backend normalizes whitespace on re-read anyway.

export default function dump(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2)
}
