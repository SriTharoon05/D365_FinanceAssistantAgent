import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ActionCard } from '../components/ActionCard';
import { Composer } from '../components/Composer';
import { ConnectionBadge, ConnectionBanner } from '../components/Connection';
import { ConversationList } from '../components/ConversationList';
import { EvidenceCards, EvidencePanel } from '../components/Evidence';
import { endpoints } from '../lib/api';
import {
  capabilities,
  connectedStatus,
  conversation,
  invoiceEvidence,
  pendingAction,
  unavailableStatus,
} from './fixtures';

function listProps() {
  return {
    conversations: [
      conversation('conversation-1', 'Customer balance review'),
      conversation('conversation-2', 'Payment journal review'),
    ],
    activeId: 'conversation-1',
    onSelect: vi.fn(),
    onNew: vi.fn(),
    onRename: vi.fn(),
    onArchive: vi.fn(),
    onDelete: vi.fn(),
  };
}

function composerProps() {
  return {
    value: '',
    onChange: vi.fn(),
    onSend: vi.fn(),
    onStop: vi.fn(),
    streaming: false,
    disabled: false,
    capabilities,
    autoSendTranscript: false,
    onError: vi.fn(),
  };
}

describe('conversation history', () => {
  it('renders saved conversations and selects an existing conversation', async () => {
    const props = listProps();
    render(<ConversationList {...props} />);

    const selected = screen.getByRole('button', { name: 'Customer balance review' });
    expect(selected).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('heading', { name: 'Today' })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Payment journal review' }));

    expect(props.onSelect).toHaveBeenCalledWith('conversation-2');
  });

  it('starts a new conversation and shows the empty history state', async () => {
    const props = { ...listProps(), conversations: [] };
    render(<ConversationList {...props} />);
    expect(screen.getByText('No conversations yet')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: /New conversation/ }));

    expect(props.onNew).toHaveBeenCalledTimes(1);
  });
});

describe('Dynamics 365 connection experience', () => {
  it('shows the disconnected reason and offers reconnect without blocking saved history', async () => {
    const onReconnect = vi.fn();
    render(
      <ConnectionBanner
        status={unavailableStatus}
        onReconnect={onReconnect}
        isReconnecting={false}
      />,
    );
    expect(screen.getByRole('status')).toHaveTextContent('Dynamics 365 is disconnected');
    expect(screen.getByText(unavailableStatus.last_error_summary!)).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'Reconnect' }));

    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  it('disables duplicate reconnect attempts while reconnecting', () => {
    render(<ConnectionBanner status={unavailableStatus} onReconnect={vi.fn()} isReconnecting />);
    expect(screen.getByRole('status')).toHaveTextContent('Connecting to Dynamics 365');
    expect(screen.getByRole('button', { name: 'Reconnecting…' })).toBeDisabled();
  });

  it('makes connection status readable without depending on color', () => {
    render(<ConnectionBadge status={connectedStatus} onClick={vi.fn()} />);
    expect(
      screen.getByRole('button', { name: 'Dynamics 365 connected. Open integration settings' }),
    ).toHaveTextContent('Connected');
  });
});

describe('financial action confirmation', () => {
  it('waits for explicit confirmation, executes once, and displays the recorded outcome', async () => {
    const actionRequest = vi
      .spyOn(endpoints, 'action')
      .mockResolvedValue({ ...pendingAction, status: 'executed' });
    const onUpdated = vi.fn();
    render(
      <ActionCard
        action={pendingAction}
        conversationId="conversation-1"
        onUpdated={onUpdated}
        onError={vi.fn()}
      />,
    );

    expect(screen.getByRole('region', { name: 'Financial action confirmation' })).toHaveTextContent(
      'EUR 123.45 unposted payment',
    );
    expect(actionRequest).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Confirm action' }));

    await waitFor(() =>
      expect(actionRequest).toHaveBeenCalledExactlyOnceWith(
        'action-1',
        'confirm',
        'conversation-1',
      ),
    );
    expect(
      await screen.findByText('The confirmed result is recorded in the audit history.'),
    ).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Confirm action' })).not.toBeInTheDocument();
    expect(onUpdated).toHaveBeenCalledTimes(1);
  });

  it('cancels a pending action and reports that no ERP change was made', async () => {
    const actionRequest = vi
      .spyOn(endpoints, 'action')
      .mockResolvedValue({ ...pendingAction, status: 'cancelled' });
    render(
      <ActionCard
        action={pendingAction}
        conversationId="conversation-1"
        onUpdated={vi.fn()}
        onError={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(await screen.findByText('Cancelled. No ERP change was made.')).toBeVisible();
    expect(actionRequest).toHaveBeenCalledExactlyOnceWith('action-1', 'cancel', 'conversation-1');
  });

  it('prevents expired confirmations and asks for a new action using current ERP data', () => {
    const actionRequest = vi.spyOn(endpoints, 'action');
    render(
      <ActionCard
        action={{ ...pendingAction, expires_at: '2000-01-01T00:00:00Z' }}
        conversationId="conversation-1"
        onUpdated={vi.fn()}
        onError={vi.fn()}
      />,
    );

    expect(screen.getByText('Confirmation expired')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Confirm action' })).not.toBeInTheDocument();
    expect(
      screen.getByText(
        'Ask the assistant to prepare a new action after reviewing current ERP data.',
      ),
    ).toBeVisible();
    expect(actionRequest).not.toHaveBeenCalled();
  });

  it('surfaces the server safety error without claiming that a mutation succeeded', async () => {
    vi.spyOn(endpoints, 'action').mockRejectedValue(
      new Error('The invoice is already posted and cannot be changed.'),
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

    await userEvent.click(screen.getByRole('button', { name: 'Confirm action' }));

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith('The invoice is already posted and cannot be changed.'),
    );
    expect(
      screen.queryByText('The confirmed result is recorded in the audit history.'),
    ).not.toBeInTheDocument();
  });
});

describe('text and voice composer', () => {
  it('keeps text chat enabled when voice transcription is unavailable', async () => {
    const props = { ...composerProps(), value: 'Find customer DEMO-001' };
    render(<Composer {...props} />);
    expect(
      screen.getByRole('button', { name: 'Voice transcription is not configured' }),
    ).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'Message Finance Assistant' })).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));

    expect(props.onSend).toHaveBeenCalledWith('Find customer DEMO-001');
    expect(props.onError).not.toHaveBeenCalled();
  });

  it('uses Enter to send, preserves Shift+Enter, and ignores IME composition', () => {
    const props = { ...composerProps(), value: '  Retrieve invoice INV-42  ' };
    render(<Composer {...props} />);
    const input = screen.getByRole('textbox', { name: 'Message Finance Assistant' });

    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    expect(props.onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(props.onSend).toHaveBeenCalledExactlyOnceWith('Retrieve invoice INV-42');
  });

  it('offers generation cancellation while the assistant response is streaming', async () => {
    const props = { ...composerProps(), streaming: true };
    render(<Composer {...props} />);
    expect(screen.queryByRole('button', { name: 'Send message' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Stop generation' }));

    expect(props.onStop).toHaveBeenCalledTimes(1);
  });
});

describe('finance source evidence', () => {
  it('renders the invoice reference, remaining balance, and company with an inspect action', async () => {
    const onOpen = vi.fn();
    render(<EvidenceCards records={[invoiceEvidence]} onOpen={onOpen} />);
    expect(screen.getByText('INV-42')).toBeVisible();
    expect(screen.getByText('EUR 123.45')).toBeVisible();
    expect(screen.getByText(/USMF/)).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: /Inspect source records/ }));

    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('provides source entity, voucher, retrieval timestamp, and full data in an accessible dialog', () => {
    render(<EvidencePanel records={[invoiceEvidence]} open onOpenChange={vi.fn()} />);
    const dialog = screen.getByRole('dialog', { name: 'ERP evidence' });
    expect(within(dialog).getByText('VerifiedCustomerOpenTransactions')).toBeVisible();
    expect(within(dialog).getByText('VCH-42')).toBeVisible();
    expect(within(dialog).getByText('Retrieved at')).toBeVisible();
    expect(within(dialog).getByText('Original amount')).toBeVisible();
    expect(within(dialog).getByText('EUR 200.00')).toBeVisible();
    expect(within(dialog).getByText('Remaining amount')).toBeVisible();
    expect(within(dialog).getByText('EUR 123.45')).toBeVisible();
    expect(within(dialog).getByText('Full source record')).toBeVisible();
  });
});
