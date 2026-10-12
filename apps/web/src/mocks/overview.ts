/**
 * ============================================================================
 * STATIC SAMPLE DATA — NOT REAL MEASUREMENTS
 * ============================================================================
 *
 * Hard-coded fixtures used solely to demonstrate the intended layout and
 * visual design of the Overview page while the real monitoring pipeline does
 * not exist.
 *
 * Engineering rules 8 and 9:
 *   - Nothing here is fetched. There is no API call behind these values, and
 *     no fake client that pretends there is.
 *   - Everything rendered from this module must sit beneath a
 *     `<MockDataBanner />` so a viewer cannot mistake it for telemetry.
 *   - The hostnames, container names and numbers are invented.
 *
 * This file is deleted in Phase 3, when agent ingestion makes the Overview
 * page read from the real API.
 */

import type { HealthStatus } from '@/components/ui/status-indicator';

/** Marker exported for tests asserting that mock data is labelled as such. */
export const IS_MOCK_DATA = true;

export type MockHost = {
  id: string;
  hostname: string;
  status: HealthStatus;
  os: string;
  /** Percent, 0-100. */
  cpuPercent: number;
  memoryPercent: number;
  diskPercent: number;
  uptime: string;
  containerCount: number;
};

export const MOCK_HOSTS: readonly MockHost[] = [
  {
    id: 'sample-host-1',
    hostname: 'sample-nas',
    status: 'ok',
    os: 'Debian 13',
    cpuPercent: 12,
    memoryPercent: 48,
    diskPercent: 71,
    uptime: '42d 6h',
    containerCount: 9,
  },
  {
    id: 'sample-host-2',
    hostname: 'sample-compute',
    status: 'warn',
    os: 'Fedora Server 44',
    cpuPercent: 68,
    memoryPercent: 86,
    diskPercent: 54,
    uptime: '11d 19h',
    containerCount: 14,
  },
  {
    id: 'sample-host-3',
    hostname: 'sample-edge',
    status: 'ok',
    os: 'Ubuntu 26.04 LTS',
    cpuPercent: 7,
    memoryPercent: 31,
    diskPercent: 22,
    uptime: '88d 2h',
    containerCount: 4,
  },
  {
    id: 'sample-host-4',
    hostname: 'sample-backup',
    status: 'unknown',
    os: 'Debian 12',
    cpuPercent: 0,
    memoryPercent: 0,
    diskPercent: 63,
    uptime: '—',
    containerCount: 0,
  },
] as const;

export type MockContainer = {
  id: string;
  name: string;
  image: string;
  host: string;
  state: 'running' | 'exited' | 'restarting';
  status: HealthStatus;
  /** Human-readable relative time. */
  since: string;
};

export const MOCK_CONTAINERS: readonly MockContainer[] = [
  {
    id: 'sample-ctr-1',
    name: 'sample-reverse-proxy',
    image: 'caddy:2-alpine',
    host: 'sample-edge',
    state: 'running',
    status: 'ok',
    since: '12d',
  },
  {
    id: 'sample-ctr-2',
    name: 'sample-postgres',
    image: 'postgres:17-alpine',
    host: 'sample-nas',
    state: 'running',
    status: 'ok',
    since: '42d',
  },
  {
    id: 'sample-ctr-3',
    name: 'sample-media-indexer',
    image: 'example/indexer:1.4.2',
    host: 'sample-compute',
    state: 'restarting',
    status: 'warn',
    since: '4m',
  },
  {
    id: 'sample-ctr-4',
    name: 'sample-metrics-exporter',
    image: 'example/exporter:0.9.1',
    host: 'sample-compute',
    state: 'exited',
    status: 'critical',
    since: '27m',
  },
] as const;

export type MockLogEntry = {
  id: string;
  /** ISO-8601 timestamp. Fixed, not generated, so renders are deterministic. */
  timestamp: string;
  level: 'ERROR' | 'WARN' | 'INFO';
  source: string;
  message: string;
};

export const MOCK_LOG_ENTRIES: readonly MockLogEntry[] = [
  {
    id: 'sample-log-1',
    timestamp: '2026-01-14T09:42:17Z',
    level: 'ERROR',
    source: 'sample-metrics-exporter',
    message: 'network unreachable: dial tcp 10.0.0.14:9100: connect: no route to host',
  },
  {
    id: 'sample-log-2',
    timestamp: '2026-01-14T09:41:58Z',
    level: 'WARN',
    source: 'sample-media-indexer',
    message: 'retry 3/5 for scheduled scan; backing off 30s',
  },
  {
    id: 'sample-log-3',
    timestamp: '2026-01-14T09:40:02Z',
    level: 'INFO',
    source: 'sample-reverse-proxy',
    message: 'certificate renewed for sample.internal, expires in 90d',
  },
  {
    id: 'sample-log-4',
    timestamp: '2026-01-14T09:38:44Z',
    level: 'ERROR',
    source: 'sample-compute/kernel',
    message: 'EXT4-fs warning: mounting fs with errors, running e2fsck is recommended',
  },
] as const;

/**
 * A fixed 24-point series for the overview sparkline.
 *
 * Static rather than randomly generated: random mock data makes the UI change
 * on every render, which looks like live telemetry and would undermine the
 * point of labelling it as a fixture.
 */
export type MockSeriesPoint = { t: string; cpu: number; memory: number };

export const MOCK_CPU_SERIES: readonly MockSeriesPoint[] = [
  { t: '00:00', cpu: 18, memory: 52 },
  { t: '01:00', cpu: 15, memory: 51 },
  { t: '02:00', cpu: 22, memory: 53 },
  { t: '03:00', cpu: 41, memory: 58 },
  { t: '04:00', cpu: 38, memory: 61 },
  { t: '05:00', cpu: 24, memory: 57 },
  { t: '06:00', cpu: 19, memory: 55 },
  { t: '07:00', cpu: 28, memory: 59 },
  { t: '08:00', cpu: 44, memory: 64 },
  { t: '09:00', cpu: 61, memory: 72 },
  { t: '10:00', cpu: 58, memory: 75 },
  { t: '11:00', cpu: 49, memory: 71 },
  { t: '12:00', cpu: 53, memory: 73 },
  { t: '13:00', cpu: 67, memory: 79 },
  { t: '14:00', cpu: 72, memory: 83 },
  { t: '15:00', cpu: 64, memory: 81 },
  { t: '16:00', cpu: 55, memory: 77 },
  { t: '17:00', cpu: 47, memory: 74 },
  { t: '18:00', cpu: 39, memory: 69 },
  { t: '19:00', cpu: 34, memory: 66 },
  { t: '20:00', cpu: 42, memory: 68 },
  { t: '21:00', cpu: 36, memory: 63 },
  { t: '22:00', cpu: 27, memory: 58 },
  { t: '23:00', cpu: 21, memory: 54 },
] as const;
