// Configuration diagnostic views API types and query hooks (ALP-684).
//
// Types mirror the Pydantic response models in
// `command_center/views/configuration.py` (06c additions).
//
// Hooks:
//   useResolvedConfig(invocationId?)  — GET /api/views/config/resolved
//   useResolvedConfigDiff(from, to)   — GET /api/views/config/resolved/diff
//   useGitHistory(file)               — GET /api/views/config/git/history
//   useGitDiff(file, fromSha, toSha)  — GET /api/views/config/git/diff
//   useGitStatus(file)                — GET /api/views/config/git/status

import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import { api } from './client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ResolvedConfigBundle = {
  invocation_id: string
  bundle: Record<string, unknown>
  source_files: Record<string, string>
}

export type ResolvedConfigDiff = {
  from_invocation_id: string
  to_invocation_id: string
  diff_lines: string[]
}

export type GitCommitEntry = {
  sha: string
  short_sha: string
  author: string
  date: string
  subject: string
}

export type GitHistoryResponse = {
  file: string
  commits: GitCommitEntry[]
}

export type GitDiffResponse = {
  file: string
  from_sha: string
  to_sha: string
  diff_text: string
}

export type GitStatusResponse = {
  file: string
  tracked: boolean
  has_uncommitted_changes: boolean
  untracked: boolean
}

// ---------------------------------------------------------------------------
// Query options
// ---------------------------------------------------------------------------

export function resolvedConfigQueryOptions(invocationId?: string) {
  const qs = invocationId ? `?invocation_id=${encodeURIComponent(invocationId)}` : ''
  return queryOptions<ResolvedConfigBundle>({
    queryKey: ['config', 'resolved', invocationId ?? 'latest'],
    queryFn: () => api.get<ResolvedConfigBundle>(`/api/views/config/resolved${qs}`),
    staleTime: 30 * 1000,
    retry: 1,
  })
}

export function resolvedConfigDiffQueryOptions(fromInvocationId: string, toInvocationId: string) {
  const qs = new URLSearchParams({
    from_invocation_id: fromInvocationId,
    to_invocation_id: toInvocationId,
  }).toString()
  return queryOptions<ResolvedConfigDiff>({
    queryKey: ['config', 'resolved', 'diff', fromInvocationId, toInvocationId],
    queryFn: () => api.get<ResolvedConfigDiff>(`/api/views/config/resolved/diff?${qs}`),
    staleTime: 5 * 60 * 1000,
    retry: 1,
  })
}

export function gitHistoryQueryOptions(file: string) {
  return queryOptions<GitHistoryResponse>({
    queryKey: ['config', 'git', 'history', file],
    queryFn: () =>
      api.get<GitHistoryResponse>(
        `/api/views/config/git/history?${new URLSearchParams({ file }).toString()}`,
      ),
    staleTime: 30 * 1000,
    retry: 1,
    enabled: file.length > 0,
  })
}

export function gitDiffQueryOptions(file: string, fromSha: string, toSha: string) {
  const qs = new URLSearchParams({ file, from_sha: fromSha, to_sha: toSha }).toString()
  return queryOptions<GitDiffResponse>({
    queryKey: ['config', 'git', 'diff', file, fromSha, toSha],
    queryFn: () => api.get<GitDiffResponse>(`/api/views/config/git/diff?${qs}`),
    staleTime: 5 * 60 * 1000,
    retry: 1,
    enabled: file.length > 0 && fromSha.length > 0 && toSha.length > 0,
  })
}

export function gitStatusQueryOptions(file: string) {
  return queryOptions<GitStatusResponse>({
    queryKey: ['config', 'git', 'status', file],
    queryFn: () =>
      api.get<GitStatusResponse>(
        `/api/views/config/git/status?${new URLSearchParams({ file }).toString()}`,
      ),
    staleTime: 15 * 1000,
    retry: 1,
    enabled: file.length > 0,
  })
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useResolvedConfig(invocationId?: string): UseQueryResult<ResolvedConfigBundle> {
  return useQuery(resolvedConfigQueryOptions(invocationId))
}

export function useResolvedConfigDiff(
  fromInvocationId: string,
  toInvocationId: string,
): UseQueryResult<ResolvedConfigDiff> {
  return useQuery(resolvedConfigDiffQueryOptions(fromInvocationId, toInvocationId))
}

export function useGitHistory(file: string): UseQueryResult<GitHistoryResponse> {
  return useQuery(gitHistoryQueryOptions(file))
}

export function useGitDiff(
  file: string,
  fromSha: string,
  toSha: string,
): UseQueryResult<GitDiffResponse> {
  return useQuery(gitDiffQueryOptions(file, fromSha, toSha))
}

export function useGitStatus(file: string): UseQueryResult<GitStatusResponse> {
  return useQuery(gitStatusQueryOptions(file))
}
