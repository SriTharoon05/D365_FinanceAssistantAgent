import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type {
  Conversation,
  EvidenceRecord,
  IntegrationStatus,
  Message,
  PendingAction,
  Preferences,
} from '../types';
import { ApiError } from '../lib/api';
import { streamChat } from '../lib/stream';
import { label } from '../lib/format';
import type { SpeechProvider } from './useSpeech';
interface Generation {
  conversationId: string;
  messageId: string;
  tool: string;
}
interface Options {
  conversationId?: string;
  model?: string;
  preferences: Preferences;
  speech: Pick<SpeechProvider, 'speak' | 'stop'>;
  createChat: () => Promise<Conversation | undefined>;
  refresh: (id?: string) => Promise<void>;
  notifyError: (message: string) => void;
  setComposer: (text: string) => void;
}
export function useChatStream({
  conversationId,
  model,
  preferences,
  speech,
  createChat,
  refresh,
  notifyError,
  setComposer,
}: Options) {
  const client = useQueryClient();
  const [generation, setGeneration] = useState<Generation | null>(null);
  const generating = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const stopGeneration = useCallback(() => controller.current?.abort(), []);
  useEffect(() => () => controller.current?.abort(), []);
  const send = async (text: string, retryMessageId?: string) => {
    if (generating.current || !text.trim()) return;
    generating.current = true;
    speech.stop();
    const id = conversationId || (await createChat())?.id;
    if (!id) {
      generating.current = false;
      return;
    }
    setComposer('');
    const control = new AbortController();
    controller.current = control;
    const assistantId = `local-${crypto.randomUUID()}`;
    const timestamp = new Date().toISOString();
    let realId = assistantId;
    let finished = false;
    let streamErrored = false;
    const assistant: Message = {
      id: assistantId,
      role: 'assistant',
      content: '',
      status: 'streaming',
      created_at: timestamp,
      model: model,
      metadata: {},
    };
    client.setQueryData<Message[]>(['messages', id], (previous = []) => [
      ...previous.filter((message) => message.id !== retryMessageId),
      ...(!retryMessageId
        ? [
            {
              id: `local-${crypto.randomUUID()}`,
              role: 'user' as const,
              content: text,
              status: 'completed',
              created_at: timestamp,
              metadata: {},
            },
          ]
        : []),
      assistant,
    ]);
    setGeneration({ conversationId: id, messageId: assistantId, tool: 'Preparing your response…' });
    const update = (apply: (message: Message) => Message) =>
      client.setQueryData<Message[]>(['messages', id], (previous = []) =>
        previous.map((message) =>
          message.id === realId || message.id === assistantId ? apply(message) : message,
        ),
      );
    try {
      await streamChat(
        {
          conversation_id: id,
          message: text,
          ...(retryMessageId ? { retry_message_id: retryMessageId } : {}),
        },
        control.signal,
        ({ event, data }) => {
          if (event === 'message_start') {
            if (typeof data.message_id === 'string') {
              realId = data.message_id;
              update((message) => ({ ...message, id: realId }));
              setGeneration((previous) => (previous ? { ...previous, messageId: realId } : null));
            }
          } else if (event === 'message_delta' && typeof data.delta === 'string') {
            update((message) => ({ ...message, content: message.content + data.delta }));
            setGeneration((previous) => (previous ? { ...previous, tool: '' } : null));
          } else if (event === 'tool_start') {
            setGeneration((previous) =>
              previous
                ? { ...previous, tool: `${label(String(data.name || 'Reading finance data'))}…` }
                : null,
            );
          } else if (event === 'tool_result') {
            setGeneration((previous) =>
              previous ? { ...previous, tool: 'Preparing an evidence-backed answer…' } : null,
            );
          } else if (event === 'evidence') {
            const records = Array.isArray(data.evidence) ? (data.evidence as EvidenceRecord[]) : [];
            update((message) => ({
              ...message,
              metadata: {
                ...message.metadata,
                evidence: [...(message.metadata.evidence || []), ...records],
              },
            }));
          } else if (event === 'pending_action' && data.action) {
            const action = data.action as PendingAction;
            update((message) => ({
              ...message,
              metadata: {
                ...message.metadata,
                pending_actions: [...(message.metadata.pending_actions || []), action],
              },
            }));
          } else if (event === 'integration_status') {
            if (typeof data.status === 'string')
              client.setQueryData<IntegrationStatus>(['integration'], (previous) =>
                previous
                  ? { ...previous, ...data, status: data.status as IntegrationStatus['status'] }
                  : previous,
              );
          } else if (event === 'error') {
            streamErrored = true;
            update((message) => ({
              ...message,
              status: 'error',
              content: String(
                data.message ||
                  'The assistant could not complete the response. Retry when the service is available.',
              ),
              error_code: String(data.code || 'ASSISTANT_UNAVAILABLE'),
            }));
          } else if (event === 'done') {
            finished = true;
            if (!streamErrored)
              update((message) => ({ ...message, status: String(data.status || 'completed') }));
          }
        },
      );
      if (!finished && !streamErrored)
        throw new ApiError(
          'The response stream ended before completion. Retry to request a complete response.',
          'STREAM_INTERRUPTED',
          undefined,
          true,
        );
      if (finished && !streamErrored && preferences.autoRead && preferences.speechEnabled) {
        const completed = client
          .getQueryData<Message[]>(['messages', id])
          ?.find((message) => message.id === realId);
        if (completed?.content) speech.speak(completed.content);
      }
    } catch (error) {
      if (control.signal.aborted)
        update((message) => ({
          ...message,
          status: 'interrupted',
          content: message.content || 'Generation stopped.',
        }));
      else {
        const failure =
          error instanceof Error
            ? error
            : new Error('The response could not be streamed. Check the connection and retry.');
        update((message) => ({
          ...message,
          status: 'error',
          content: failure.message,
          error_code: error instanceof ApiError ? error.code : 'NETWORK_ERROR',
        }));
        notifyError(failure.message);
        if (!retryMessageId) setComposer(text);
      }
    } finally {
      controller.current = null;
      generating.current = false;
      setGeneration(null);
      if (!control.signal.aborted) await refresh(id);
    }
  };
  const retry = (messageId: string) => {
    const history = client.getQueryData<Message[]>(['messages', conversationId]) || [];
    const index = history.findIndex((message) => message.id === messageId);
    const user = history
      .slice(0, index + 1)
      .reverse()
      .find((message) => message.role === 'user');
    if (user) void send(user.content, messageId);
  };
  return { generation, generating, send, retry, stopGeneration };
}
