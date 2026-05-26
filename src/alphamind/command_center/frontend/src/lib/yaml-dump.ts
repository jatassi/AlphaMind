// yaml-dump — YAML serialization for config editor pages (ALP-682).
//
// Wraps js-yaml's safe ``dump`` so editor pages emit real YAML on save
// (not JSON-shaped text). The backend ``yaml.safe_load`` accepts JSON-
// subset payloads transparently, but downstream tooling (operators
// reading ``git diff`` on profile YAML, the git-history viewer's
// per-line diff) treats the file as YAML and a flattened JSON dump
// loses the multi-document shape + key ordering operators rely on.
//
// ``noRefs: true`` prevents anchor/alias emission — profile/regime YAML
// is always tree-shaped, never has repeated subtrees that need
// deduplication, and an anchor in the output would surprise the
// operator on next read.

import * as yaml from 'js-yaml'

export default function dump(value: Record<string, unknown>): string {
  return yaml.dump(value, { noRefs: true })
}
