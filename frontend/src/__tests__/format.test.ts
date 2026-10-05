import { describe, expect, it } from 'vitest';
import { money } from '../lib/format';

describe('financial display values', () => {
  it('preserves decimal precision and large ERP amounts without a float conversion', () => {
    expect(money('9007199254740993.123400', 'EUR')).toBe('EUR 9,007,199,254,740,993.123400');
  });

  it('keeps negative credit balances and explicit zero distinguishable from missing evidence', () => {
    expect(money('-1234.50', 'GBP')).toBe('GBP -1,234.50');
    expect(money('0.00', 'GBP')).toBe('GBP 0.00');
    expect(money(undefined, 'GBP')).toBe('—');
  });
});
