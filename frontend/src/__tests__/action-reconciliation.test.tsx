import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ActionCard } from '../components/ActionCard';
import { endpoints } from '../lib/api';
import type { PendingAction } from '../types';
import { pendingAction } from './fixtures';

const unknownUpdate: PendingAction = {
  ...pendingAction,
  action_type: 'update_customer',
  title: 'Update TEST-CHAT-001?',
  status: 'unknown',
  result: {
    message: 'The customer update could not be verified after the request completed.',
    cause_code: 'd365_response_invalid',
  },
};

function actionProps(action = unknownUpdate) {
  return {
    action,
    conversationId: 'conversation-1',
    onUpdated: vi.fn(),
    onError: vi.fn(),
  };
}

describe('financial action outcome verification', () => {
  it('checks an unknown update once without repeating the write and describes the verified state', async () => {
    let finish!: (value: Pick<PendingAction, 'id' | 'status' | 'result'>) => void;
    const actionRequest = vi.spyOn(endpoints, 'action').mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const props = actionProps();
    render(<ActionCard {...props} />);

    expect(screen.getByText(unknownUpdate.result!.message as string)).toBeVisible();
    expect(screen.getByText('Error code: d365_response_invalid')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Confirm action' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Verify in D365' }));

    const checking = screen.getByRole('button', { name: 'Checking…' });
    expect(checking).toBeDisabled();
    await userEvent.click(checking);
    expect(actionRequest).toHaveBeenCalledExactlyOnceWith('action-1', 'verify', 'conversation-1');

    finish({
      id: 'action-1',
      status: 'executed',
      result: { result: { identifier: 'TEST-CHAT-001', reconciled: true } },
    });

    expect(
      await screen.findByText(
        'The current record state was verified in Dynamics 365 and recorded in the audit history.',
      ),
    ).toBeVisible();
    expect(screen.getByText('TEST-CHAT-001')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Verify in D365' })).not.toBeInTheDocument();
    expect(
      screen.queryByText('The confirmed result is recorded in the audit history.'),
    ).not.toBeInTheDocument();
    expect(props.onUpdated).toHaveBeenCalledTimes(1);
    expect(props.onError).not.toHaveBeenCalled();
  });

  it('refreshes the saved action after an inconclusive verification and keeps the outcome uncertain', async () => {
    const actionRequest = vi
      .spyOn(endpoints, 'action')
      .mockRejectedValue(new Error('The customer still has a different name in Dynamics 365.'));
    const props = actionProps({ ...unknownUpdate, status: 'verification_required' });
    render(<ActionCard {...props} />);

    await userEvent.click(screen.getByRole('button', { name: 'Verify in D365' }));

    await waitFor(() =>
      expect(props.onError).toHaveBeenCalledWith(
        'The customer still has a different name in Dynamics 365.',
      ),
    );
    expect(actionRequest).toHaveBeenCalledExactlyOnceWith('action-1', 'verify', 'conversation-1');
    expect(props.onUpdated).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Verify in D365' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Confirm action' })).not.toBeInTheDocument();
    expect(screen.getByText(/The write outcome is uncertain/)).toBeVisible();
  });

  it('shows the definitive failure reason safely without asking to verify or claiming success', () => {
    const message = 'Dynamics 365 rejected this update: <script>alert("error")</script>';
    render(
      <ActionCard
        {...actionProps({
          ...unknownUpdate,
          status: 'failed',
          result: { message, code: 'd365_validation_error' },
        })}
      />,
    );

    expect(screen.getByText(message)).toBeVisible();
    expect(screen.getByText('Error code: d365_validation_error')).toBeVisible();
    expect(
      screen.getByText(
        'The action did not complete. Review the error before preparing a new action.',
      ),
    ).toBeVisible();
    expect(document.querySelector('script')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Verify in D365' })).not.toBeInTheDocument();
    expect(screen.queryByText(/The write outcome is uncertain/)).not.toBeInTheDocument();
  });

  it.each(['pending', 'executing', 'cancelled', 'executed'])(
    'does not offer outcome verification for a %s action',
    (status) => {
      render(<ActionCard {...actionProps({ ...pendingAction, status })} />);
      expect(screen.queryByRole('button', { name: 'Verify in D365' })).not.toBeInTheDocument();
    },
  );

  it('requires manual verification for an uncertain create operation without repeating it', () => {
    const actionRequest = vi.spyOn(endpoints, 'action');
    render(<ActionCard {...actionProps({ ...unknownUpdate, action_type: 'create_customer' })} />);

    expect(screen.getByText(/Check the affected record directly in Dynamics 365/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Verify in D365' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm action' })).not.toBeInTheDocument();
    expect(actionRequest).not.toHaveBeenCalled();
  });

  it('sends the conversation-scoped read-only verification request to the verify endpoint', async () => {
    const fetchRequest = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        new Response(
          JSON.stringify({ id: 'action/1', status: 'executed', result: { reconciled: true } }),
          { headers: { 'Content-Type': 'application/json' } },
        ),
      );

    const result = await endpoints.action('action/1', 'verify', 'conversation-1');

    expect(result.status).toBe('executed');
    expect(fetchRequest).toHaveBeenCalledExactlyOnceWith('/api/actions/action%2F1/verify', {
      credentials: 'include',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: 'conversation-1' }),
    });
  });
});
