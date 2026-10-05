import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../lib/api';
import { readEventStream, streamChat, type StreamEvent } from '../lib/stream';

function chunkedResponse(text: string, chunkSize = 1): Response {
  const bytes = new TextEncoder().encode(text);
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (let offset = 0; offset < bytes.length; offset += chunkSize) {
          controller.enqueue(bytes.slice(offset, offset + chunkSize));
        }
        controller.close();
      },
    }),
    { headers: { 'Content-Type': 'text/event-stream' } },
  );
}

describe('finance chat event stream', () => {
  it('preserves ordered UTF-8 deltas across arbitrary network byte boundaries', async () => {
    const response = chunkedResponse(
      ': heartbeat\r\n\r\n' +
        'event: message_start\r\ndata: {"message_id":"assistant-1"}\r\n\r\n' +
        'event: message_delta\r\ndata: {"delta":"₹ balance "}\r\n\r\n' +
        'event: message_delta\r\ndata: {"delta":"retrieved ✓"}\r\n\r\n' +
        'event: done\r\ndata: {"message_id":"assistant-1"}',
    );
    const events: StreamEvent[] = [];

    await readEventStream(response, (event) => events.push(event));

    expect(events.map((event) => event.event)).toEqual([
      'message_start',
      'message_delta',
      'message_delta',
      'done',
    ]);
    expect(
      events
        .filter((event) => event.event === 'message_delta')
        .map((event) => event.data.delta)
        .join(''),
    ).toBe('₹ balance retrieved ✓');
  });

  it('delivers evidence and pending actions without treating their data as assistant text', async () => {
    const events: StreamEvent[] = [];
    await readEventStream(
      chunkedResponse(
        'event: evidence\ndata: {"records":[{"company":"USMF","invoice_number":"INV-42","remaining_amount":"123.45"}]}\n\n' +
          'event: pending_action\ndata: {"action":{"id":"action-1","status":"pending"}}\n\n',
        7,
      ),
      (event) => events.push(event),
    );

    expect(events[0].data.records).toEqual([
      { company: 'USMF', invoice_number: 'INV-42', remaining_amount: '123.45' },
    ]);
    expect(events[1]).toEqual({
      event: 'pending_action',
      data: { action: { id: 'action-1', status: 'pending' } },
    });
  });

  it('returns an actionable retryable error for malformed server events', async () => {
    await expect(
      readEventStream(chunkedResponse('event: message_delta\ndata: {invalid json}\n\n'), vi.fn()),
    ).rejects.toMatchObject({
      name: 'ApiError',
      code: 'INVALID_STREAM',
      retryable: true,
    });
  });

  it('surfaces integration error details instead of starting an invalid stream', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: 'D365_DISCONNECTED',
            message: 'Dynamics 365 is unavailable. Reconnect to continue.',
            request_id: 'request-42',
            retryable: true,
          },
        }),
        { status: 503, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const onEvent = vi.fn();

    await expect(
      streamChat(
        { conversation_id: 'conversation-1', message: 'Show live balance' },
        new AbortController().signal,
        onEvent,
      ),
    ).rejects.toMatchObject({
      message: 'Dynamics 365 is unavailable. Reconnect to continue.',
      code: 'D365_DISCONNECTED',
      requestId: 'request-42',
    } satisfies Partial<ApiError>);
    expect(onEvent).not.toHaveBeenCalled();
  });

  it('passes the cancellation signal and message to the streaming request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(chunkedResponse('event: done\ndata: {}\n\n'));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    await streamChat(
      { conversation_id: 'conversation-1', message: 'Find customer DEMO-001' },
      controller.signal,
      vi.fn(),
    );

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chat/stream',
      expect.objectContaining({
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          conversation_id: 'conversation-1',
          message: 'Find customer DEMO-001',
        }),
      }),
    );
  });
});
