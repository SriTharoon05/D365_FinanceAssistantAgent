import { describe, expect, it } from 'vitest';
import { connectionProgress, connectionTiming, integrationPollInterval } from '../lib/integration';
import type { ConnectionState, IntegrationStatus } from '../types';
import { connectedStatus } from './fixtures';

describe('Dynamics 365 health polling', () => {
  it.each<ConnectionState>(['connecting', 'reconnecting'])(
    'checks an active %s operation every two seconds',
    (status) => {
      expect(integrationPollInterval(status)).toBe(2_000);
    },
  );

  it.each<ConnectionState | undefined>(['connected', 'disconnected', 'degraded', undefined])(
    'checks a settled or unknown %s state every thirty seconds',
    (status) => {
      expect(integrationPollInterval(status)).toBe(30_000);
    },
  );
});

describe('Dynamics 365 connection progress budgets', () => {
  it('separates total time from the longer metadata stage budget', () => {
    expect(
      connectionTiming({
        ...connectedStatus,
        connection_elapsed_seconds: 75,
        connection_phase_elapsed_seconds: 70,
        connection_phase_timeout_seconds: 180,
        connection_timeout_seconds: 240,
      }),
    ).toBe('Total elapsed 75s · Stage 70s / 180s · Overall limit 240s');
  });

  it.each<{ fields: Partial<IntegrationStatus>; expected: string }>([
    { fields: {}, expected: '' },
    { fields: { connection_timeout_seconds: 240 }, expected: 'Overall limit 240s' },
    {
      fields: { connection_elapsed_seconds: 75, connection_phase_elapsed_seconds: 70 },
      expected: 'Total elapsed 75s · Stage elapsed 70s',
    },
    { fields: { connection_phase_timeout_seconds: 180 }, expected: 'Stage limit 180s' },
  ])('displays only supplied time budgets: $expected', ({ fields, expected }) => {
    expect(connectionTiming({ ...connectedStatus, ...fields })).toBe(expected);
  });

  it('omits invalid or missing timings instead of inventing elapsed time or a deadline', () => {
    expect(
      connectionTiming({
        ...connectedStatus,
        connection_elapsed_seconds: Number.NaN,
        connection_timeout_seconds: Number.POSITIVE_INFINITY,
        connection_phase_elapsed_seconds: null,
        connection_phase_timeout_seconds: null,
      }),
    ).toBe('');
  });

  it('uses the documented three-minute first-download budget when the optional stage budget is absent', () => {
    expect(connectionProgress({ ...connectedStatus, connection_stage: 'loading_metadata' })).toBe(
      'Loading Dynamics 365 OData metadata. The first download may take up to 3 minutes.',
    );
  });

  it('uses readable verification progress for unrecognized stage names including prototype keys', () => {
    expect(connectionProgress({ ...connectedStatus, connection_stage: '__proto__' })).toBe(
      'Verifying Microsoft Entra authentication and available Dynamics 365 finance entities.',
    );
  });
});
