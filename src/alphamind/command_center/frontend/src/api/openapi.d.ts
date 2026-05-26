// Placeholder for openapi-typescript codegen output. The real types are
// regenerated via `bun run generate-types`, which hits the running FastAPI
// daemon's /openapi.json and writes the schema into this file.
//
// Until per-view stories land their API surfaces, this stub keeps the
// import surface stable so `@/api/openapi` resolves at typecheck + lint
// time. View stories run `generate-types` and commit the regenerated file
// alongside their endpoint additions.
//
// The lowercase type-alias names are openapi-typescript's mandatory output
// convention — every codegen output uses `paths`, `components`,
// `operations`. The naming-convention rule is disabled file-wide rather
// than perpetually re-violated by each codegen pass.

/* eslint-disable @typescript-eslint/naming-convention */
export type paths = Record<string, never>
export type components = {
  schemas: Record<string, never>
}
export type operations = Record<string, never>
