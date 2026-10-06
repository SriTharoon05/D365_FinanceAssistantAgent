import { describe, expect, it } from 'vitest';
import { integrationPollInterval } from '../lib/integration';
import type { ConnectionState } from '../types';

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
