import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MessageView } from '../components/MessageView';
import type { Message } from '../types';
import { invoiceEvidence } from './fixtures';

function props(message: Partial<Message> = {}) {
  return {
    message: {
      id: 'assistant-1',
      role: 'assistant',
      content: 'Reading live finance data',
      status: 'streaming',
      model: 'gpt-4.1-mini',
      created_at: '2026-10-05T08:00:00Z',
      metadata: {},
      ...message,
    } as Message,
    conversationId: 'conversation-1',
    onEvidence: vi.fn(),
    onRetry: vi.fn(),
    onSpeak: vi.fn(),
    canSpeak: false,
    streaming: true,
    onUpdated: vi.fn(),
    onError: vi.fn(),
  };
}

describe('assistant message rendering', () => {
  it('updates partial streamed text and exposes source evidence when the response completes', async () => {
    const initial = props();
    const { rerender } = render(<MessageView {...initial} />);
    expect(screen.getByText('Reading live finance data')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Regenerate response' })).not.toBeInTheDocument();

    const complete = {
      ...initial,
      streaming: false,
      message: {
        ...initial.message,
        status: 'completed',
        content: 'Example Services Ltd has **EUR 123.45** outstanding in USMF.',
        metadata: { evidence: [invoiceEvidence] },
      },
    };
    rerender(<MessageView {...complete} />);
    expect(screen.queryByText('Reading live finance data')).not.toBeInTheDocument();
    expect(screen.getAllByText('EUR 123.45').length).toBeGreaterThan(0);
    expect(screen.getByText('INV-42')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'View evidence' }));

    expect(initial.onEvidence).toHaveBeenCalledWith([invoiceEvidence]);
  });

  it('shows provider failure details and lets the user retry the preserved message', async () => {
    const error = props({
      content:
        'Azure OpenAI is unavailable. Your question was saved; retry when the service is ready.',
      status: 'error',
      error_code: 'AZURE_OPENAI_UNAVAILABLE',
    });
    render(<MessageView {...error} streaming={false} />);
    expect(screen.getByRole('alert')).toHaveTextContent('Your question was saved');
    expect(screen.getByRole('alert')).toHaveTextContent('AZURE_OPENAI_UNAVAILABLE');

    await userEvent.click(screen.getByRole('button', { name: 'Regenerate response' }));

    expect(error.onRetry).toHaveBeenCalledWith('assistant-1');
  });

  it('renders ERP text as safe markdown and does not insert raw HTML into the page', () => {
    const { container } = render(
      <MessageView
        {...props({ content: '<img src="x" onerror="alert(1)">\n\nVerified invoice **INV-42**.' })}
        streaming={false}
      />,
    );
    expect(screen.getByText('INV-42')).toBeVisible();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('[onerror]')).toBeNull();
  });
});
