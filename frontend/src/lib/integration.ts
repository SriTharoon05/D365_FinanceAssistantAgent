import type { ConnectionState, IntegrationStatus } from '../types';

export function integrationPollInterval(status?: ConnectionState) {
  return status === 'connecting' || status === 'reconnecting' ? 2_000 : 30_000;
}

const stages: Record<string, string> = {
  authenticating: 'Verifying Microsoft Entra authentication.',
  checking_customers: 'Checking authorized access to Dynamics 365 customer records.',
  loading_cached_metadata:
    'Loading the cached Dynamics 365 schema. Authentication and current data permissions are verified against Dynamics 365 for this connection.',
  discovering_entities: 'Discovering and validating available finance entities.',
  ready: 'Connection checks are complete. Finishing the request.',
};

function duration(seconds: number) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  const parts: string[] = [];
  if (minutes) parts.push(`${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`);
  if (remainder) parts.push(`${remainder} ${remainder === 1 ? 'second' : 'seconds'}`);
  return parts.join(' ') || '0 seconds';
}

export function connectionProgress(status: IntegrationStatus) {
  if (status.connection_stage === 'loading_metadata') {
    const configured = status.connection_phase_timeout_seconds;
    const budget =
      typeof configured === 'number' && Number.isFinite(configured) && configured > 0
        ? Math.ceil(configured)
        : 180;
    return `Loading Dynamics 365 OData metadata. The first download may take up to ${duration(budget)}.`;
  }
  return Object.hasOwn(stages, status.connection_stage || '')
    ? stages[status.connection_stage!]
    : 'Verifying Microsoft Entra authentication and available Dynamics 365 finance entities.';
}

function seconds(value?: number | null) {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.max(0, Math.floor(value))
    : undefined;
}

export function connectionTiming(status: IntegrationStatus) {
  const totalElapsed = seconds(status.connection_elapsed_seconds);
  const totalTimeout = seconds(status.connection_timeout_seconds);
  const phaseElapsed = seconds(status.connection_phase_elapsed_seconds);
  const phaseTimeout = seconds(status.connection_phase_timeout_seconds);
  const parts: string[] = [];
  if (totalElapsed !== undefined) parts.push(`Total elapsed ${totalElapsed}s`);
  if (phaseElapsed !== undefined && phaseTimeout !== undefined)
    parts.push(`Stage ${phaseElapsed}s / ${phaseTimeout}s`);
  else if (phaseElapsed !== undefined) parts.push(`Stage elapsed ${phaseElapsed}s`);
  else if (phaseTimeout !== undefined) parts.push(`Stage limit ${phaseTimeout}s`);
  if (totalTimeout !== undefined) parts.push(`Overall limit ${totalTimeout}s`);
  return parts.join(' · ');
}
