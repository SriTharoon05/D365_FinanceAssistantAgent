import type { ConnectionState, IntegrationStatus } from '../types';

export function integrationPollInterval(status?: ConnectionState) {
  return status === 'connecting' || status === 'reconnecting' ? 2_000 : 30_000;
}

const stages: Record<string, string> = {
  authenticating: 'Verifying Microsoft Entra authentication.',
  checking_customers: 'Checking authorized access to Dynamics 365 customer records.',
  loading_metadata: 'Loading Dynamics 365 OData metadata.',
  discovering_entities: 'Discovering and validating available finance entities.',
  ready: 'Connection checks are complete. Finishing the request.',
};

export function connectionProgress(status: IntegrationStatus) {
  return (
    stages[status.connection_stage || ''] ||
    'Verifying Microsoft Entra authentication and available Dynamics 365 finance entities.'
  );
}

export function connectionTiming(status: IntegrationStatus) {
  const elapsed = status.connection_elapsed_seconds;
  const timeout = status.connection_timeout_seconds;
  const parts: string[] = [];
  if (typeof elapsed === 'number' && Number.isFinite(elapsed))
    parts.push(`Elapsed ${Math.max(0, Math.floor(elapsed))}s`);
  if (typeof timeout === 'number' && Number.isFinite(timeout))
    parts.push(`Time limit ${Math.max(0, Math.floor(timeout))}s`);
  return parts.join(' · ');
}
