import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { ActionCard } from '../components/ActionCard';
import { Settings } from '../components/Settings';
import { capabilities, connectedStatus, pendingAction } from './fixtures';

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('ERP action API response regressions', () => {
  it('preserves action details after a minimal confirmation response and renders the nested ERP result', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: pendingAction.id,
        status: 'executed',
        result: {
          action_type: pendingAction.action_type,
          company: 'USMF',
          entity: pendingAction.entity,
          result: {
            identifier: 'DEMO-JOURNAL-01',
            manual_instructions: 'Review and post the unposted journal in Dynamics 365.',
            verified: true,
            posted: false,
          },
        },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const onError = vi.fn();
    const onUpdated = vi.fn();
    render(
      <ActionCard
        action={pendingAction}
        conversationId="conversation-1"
        onUpdated={onUpdated}
        onError={onError}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Confirm action' }));

    expect(await screen.findByText('DEMO-JOURNAL-01')).toBeVisible();
    expect(screen.getByText('Review and post the unposted journal in Dynamics 365.')).toBeVisible();
    expect(screen.getByRole('heading', { name: pendingAction.title })).toBeVisible();
    expect(screen.getByText(pendingAction.entity)).toBeVisible();
    expect(screen.getByText(pendingAction.description)).toBeVisible();
    expect(screen.getByText('Financial impact: EUR 123.45 unposted payment')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Confirm action' })).not.toBeInTheDocument();
    expect(onError).not.toHaveBeenCalled();
    expect(onUpdated).toHaveBeenCalledTimes(1);
  });

  it('preserves the original confirmation fields after a minimal cancellation response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({
          id: pendingAction.id,
          status: 'cancelled',
          result: null,
        }),
      ),
    );
    const onError = vi.fn();
    render(
      <ActionCard
        action={pendingAction}
        conversationId="conversation-1"
        onUpdated={vi.fn()}
        onError={onError}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByText('Cancelled. No ERP change was made.')).toBeVisible();
    expect(screen.getByRole('heading', { name: pendingAction.title })).toBeVisible();
    expect(screen.getByText(pendingAction.entity)).toBeVisible();
    expect(screen.getByText(pendingAction.description)).toBeVisible();
    expect(screen.getByText('High risk')).toBeVisible();
    expect(onError).not.toHaveBeenCalled();
  });
});

describe('D365 metadata settings regressions', () => {
  it('renders nested entity resolutions and diagnostic arrays as safe text', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
    render(
      <QueryClientProvider client={client}>
        <Settings
          open
          onOpenChange={vi.fn()}
          preferences={{
            theme: 'system',
            speechEnabled: true,
            autoRead: false,
            speechVoice: '',
            speechRate: 1,
            autoSendTranscript: false,
          }}
          updatePreferences={vi.fn()}
          status={{
            ...connectedStatus,
            status: 'degraded',
            capabilities: {
              customers: true,
              open_transactions: false,
              resolved_entities: {
                customers: 'CustomersV3',
                customer_transactions: 'VerifiedCustomerTransactions',
                open_transactions: null,
              },
              diagnostics: [
                'No suitable open-transaction entity was found.',
                'Configure D365_OPEN_TRANSACTIONS_ENTITY explicitly.',
              ],
            },
          }}
          capabilities={capabilities}
          onReconnect={vi.fn()}
          reconnecting={false}
          voices={[]}
          speechSupported={false}
          onError={vi.fn()}
          onSuccess={vi.fn()}
        />
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Dynamics 365' }));

    const dialog = screen.getByRole('dialog', { name: 'Workspace settings' });
    await waitFor(() => expect(within(dialog).getByText('Resolved entities')).toBeVisible());
    expect(within(dialog).getByText(/VerifiedCustomerTransactions/)).toBeVisible();
    expect(within(dialog).getByText(/No suitable open-transaction entity was found/)).toBeVisible();
    expect(
      within(dialog).getByText(/Configure D365_OPEN_TRANSACTIONS_ENTITY explicitly/),
    ).toBeVisible();
    expect(within(dialog).getByRole('button', { name: 'Reconnect Dynamics 365' })).toBeEnabled();
    expect(within(dialog).getByText('Unavailable')).toBeVisible();
  });
});
