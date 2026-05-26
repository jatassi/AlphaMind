// API hooks for the config editor framework (ALP-679) + profiles/regimes (ALP-682).
//
// Covers two backend surfaces used by the editor pages:
//
// * GET /api/views/config/files?family=<family>  — file-picker slug list.
// * GET /api/views/config/schema/<slug>          — FormSchema for a file.
//
// The PUT surface is exercised by SaveAction directly via api.put and
// does not need a query hook (one-shot mutation, not a cached read).

import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { FormSchema } from '@/views/configuration/framework/types'

import { api } from './client'

// ── ConfigFileList ────────────────────────────────────────────────────────────

export type ConfigFileListResponse = {
  family: string
  slugs: readonly string[]
}

export function configFileListQueryOptions(
  family: string,
): ReturnType<typeof queryOptions<ConfigFileListResponse>> {
  return queryOptions<ConfigFileListResponse>({
    queryKey: ['config', 'files', family],
    queryFn: () => api.get<ConfigFileListResponse>(`/api/views/config/files?family=${family}`),
  })
}

export function useConfigFileList(family: string): UseQueryResult<ConfigFileListResponse> {
  return useQuery(configFileListQueryOptions(family))
}

// ── FormSchema ────────────────────────────────────────────────────────────────

export function configSchemaQueryOptions(
  slug: string,
): ReturnType<typeof queryOptions<FormSchema>> {
  return queryOptions<FormSchema>({
    queryKey: ['config', 'schema', slug],
    queryFn: () => api.get<FormSchema>(`/api/views/config/schema/${slug}`),
  })
}

export function useConfigSchema(slug: string): UseQueryResult<FormSchema> {
  return useQuery(configSchemaQueryOptions(slug))
}
