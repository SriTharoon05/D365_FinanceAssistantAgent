import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import App from '../App';
import { endpoints } from '../lib/api';
import type { Conversation, Message } from '../types';
import {
  capabilities,
  connectedStatus,
  conversation,
  invoiceEvidence,
  pendingAction,
  unavailableStatus,
} from './fixtures';

function renderApp(path = '/') {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/" element={<App />} />
          <Route path="/chat/:conversationId" element={<App />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mockBackend(initialHistory: Message[] = []) {
  let savedConversations: Conversation[] = [
    conversation('conversation-1', 'Customer balance review'),
  ];
  let history = initialHistory;
  vi.spyOn(endpoints, 'capabilities').mockResolvedValue(capabilities);
  vi.spyOn(endpoints, 'status').mockResolvedValue(connectedStatus);
  vi.spyOn(endpoints, 'conversations').mockImplementation(async () => savedConversations);
  vi.spyOn(endpoints, 'conversation').mockImplementation(async (id) => {
    const found = savedConversations.find((item) => item.id === id);
    if (!found) throw new Error('Conversation not found');
    return found;
  });
  vi.spyOn(endpoints, 'messages').mockImplementation(async () => history);
  const create = vi.spyOn(endpoints, 'createConversation').mockImplementation(async () => {
    const created = conversation('conversation-2', 'New finance conversation');
    savedConversations = [...savedConversations, created];
    return created;
  });
  return {
    create,
    saveMessages: (messages: Message[]) => {
      history = messages;
    },
  };
}

function message(id: string, role: 'user' | 'assistant', content: string): Message {
  return {
    id,
    role,
    content,
    status: 'completed',
    created_at: '2026-10-05T08:00:00Z',
    metadata: {},
  };
}

describe('Finance Assistant application', () => {
  it('waits for the first session bootstrap and deduplicates immediate new-conversation clicks', async () => {
    const backend = mockBackend();
    let finishBootstrap!: (conversations: Conversation[]) => void;
    const firstPage = new Promise<Conversation[]>((resolve) => {
      finishBootstrap = resolve;
    });
    vi.mocked(endpoints.conversations).mockImplementationOnce(() => firstPage);
    renderApp();

    await userEvent.dblClick(screen.getByRole('button', { name: /New conversation/ }));

    expect(endpoints.conversations).toHaveBeenCalledTimes(1);
    expect(backend.create).not.toHaveBeenCalled();
    await act(async () => {
      finishBootstrap([conversation('conversation-1', 'Customer balance review')]);
    });

    expect(await screen.findByRole('heading', { name: 'New finance conversation' })).toBeVisible();
    expect(backend.create).toHaveBeenCalledTimes(1);
  });

  it('waits for the same session bootstrap before creating a conversation for the first message', async () => {
    const backend = mockBackend();
    let finishBootstrap!: (conversations: Conversation[]) => void;
    const firstPage = new Promise<Conversation[]>((resolve) => {
      finishBootstrap = resolve;
    });
    vi.mocked(endpoints.conversations).mockImplementationOnce(() => firstPage);
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(
          'event: message_start\ndata: {"message_id":"assistant-1"}\n\n' +
            'event: message_delta\ndata: {"delta":"No current ERP facts are fabricated."}\n\n' +
            'event: done\ndata: {"status":"completed"}\n\n',
          { headers: { 'Content-Type': 'text/event-stream' } },
        ),
      );
    vi.stubGlobal('fetch', fetchMock);
    renderApp();
    expect(screen.getByRole('textbox', { name: 'Message Finance Assistant' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send message' })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));

    expect(endpoints.conversations).toHaveBeenCalledTimes(1);
    expect(backend.create).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    backend.saveMessages([
      message('user-1', 'user', 'Find customer DEMO-001'),
      message('assistant-1', 'assistant', 'No current ERP facts are fabricated.'),
    ]);
    await act(async () => {
      finishBootstrap([conversation('conversation-1', 'Customer balance review')]);
    });
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Message Finance Assistant' })).toBeEnabled(),
    );
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Message Finance Assistant' }),
      'Find customer DEMO-001',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => expect(backend.create).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/chat/stream',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            conversation_id: 'conversation-2',
            message: 'Find customer DEMO-001',
          }),
        }),
      ),
    );
    expect(await screen.findByText('No current ERP facts are fabricated.')).toBeVisible();
  });

  it('loads persisted history and creates a server-backed new conversation', async () => {
    const backend = mockBackend();
    renderApp();
    expect(await screen.findByRole('button', { name: 'Customer balance review' })).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: /New conversation/ }));

    expect(await screen.findByRole('heading', { name: 'New finance conversation' })).toBeVisible();
    expect(backend.create).toHaveBeenCalledExactlyOnceWith('USMF');
    expect(screen.getByRole('button', { name: 'New finance conversation' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('sends a message, renders stream updates, and keeps confirmed server history and evidence', async () => {
    const backend = mockBackend();
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            streamController = controller;
          },
        }),
        {
          headers: { 'Content-Type': 'text/event-stream' },
        },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const emit = (event: string, data: Record<string, unknown>) => {
      streamController.enqueue(
        new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`),
      );
    };
    renderApp('/chat/conversation-1');
    await screen.findByRole('heading', { name: 'Customer balance review' });
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Message Finance Assistant' })).toBeEnabled(),
    );
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Message Finance Assistant' }),
      'Show customer DEMO-001 balance',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Send message' }));
    expect(await screen.findByText('Show customer DEMO-001 balance')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Stop generation' })).toBeVisible();

    await act(async () => {
      emit('message_start', { message_id: 'assistant-1' });
      emit('message_delta', { delta: 'Example Services Ltd has ' });
    });
    expect(await screen.findByText('Example Services Ltd has')).toBeVisible();
    await act(async () => {
      emit('message_delta', { delta: 'EUR 123.45 outstanding in USMF.' });
      emit('evidence', { evidence: [invoiceEvidence] });
      emit('pending_action', { action: pendingAction });
    });
    expect(await screen.findByText('INV-42')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Confirm action' })).toBeVisible();

    const persistedAssistant = {
      ...message(
        'assistant-1',
        'assistant',
        'Example Services Ltd has EUR 123.45 outstanding in USMF.',
      ),
      metadata: { evidence: [invoiceEvidence], pending_actions: [pendingAction] },
    };
    backend.saveMessages([
      message('user-1', 'user', 'Show customer DEMO-001 balance'),
      persistedAssistant,
    ]);
    await act(async () => {
      emit('done', { status: 'completed' });
      streamController.close();
    });

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Stop generation' })).not.toBeInTheDocument(),
    );
    expect(
      screen.getByText('Example Services Ltd has EUR 123.45 outstanding in USMF.'),
    ).toBeVisible();
    expect(screen.getByText('Show customer DEMO-001 balance')).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chat/stream',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          conversation_id: 'conversation-1',
          message: 'Show customer DEMO-001 balance',
        }),
      }),
    );
  });

  it('lets users view saved messages while disconnected and reconnects the live integration', async () => {
    mockBackend([
      message('user-1', 'user', 'Review prior finance question'),
      message('assistant-1', 'assistant', 'This is a saved conversation response.'),
    ]);
    vi.mocked(endpoints.status).mockResolvedValue(unavailableStatus);
    const reconnect = vi.spyOn(endpoints, 'reconnect').mockResolvedValue(connectedStatus);
    renderApp('/chat/conversation-1');
    expect(await screen.findByText('Dynamics 365 is disconnected')).toBeVisible();
    expect(await screen.findByText('This is a saved conversation response.')).toBeVisible();

    await userEvent.click(screen.getByRole('button', { name: 'Reconnect' }));

    expect(
      await screen.findByRole('button', {
        name: 'Dynamics 365 connected. Open integration settings',
      }),
    ).toBeVisible();
    expect(reconnect).toHaveBeenCalledTimes(1);
    expect(screen.getByText('This is a saved conversation response.')).toBeVisible();
  });
});
