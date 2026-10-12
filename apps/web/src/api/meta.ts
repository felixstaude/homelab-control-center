/**
 * Server metadata.
 *
 * `GET /api/v1/info` has existed since Phase 0 but had no caller: the version
 * and environment moved behind a session in milestone 1.5 (ADR-032), and
 * nothing in the interface displayed them. The General settings section is the
 * first place they belong.
 */

import { apiRequest } from '@/api/client';
import type { ApiInfo } from '@/api/types';

export function fetchApiInfo(signal?: AbortSignal): Promise<ApiInfo> {
  return apiRequest<ApiInfo>('/info', { signal });
}
