import { API_BASE, ApiError, responseError } from './api';
export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}
// The buffer is retained across byte boundaries, including split UTF-8 characters.
export async function readEventStream(response: Response, onEvent: (event: StreamEvent) => void) {
  if (!response.body)
    throw new ApiError(
      'The server did not provide a response stream.',
      'STREAM_UNAVAILABLE',
      undefined,
      true,
    );
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const dispatch = (block: string) => {
    let event = 'message';
    const lines: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      if (line.startsWith('data:')) lines.push(line.slice(5).trimStart());
    }
    if (lines.length) {
      try {
        onEvent({ event, data: JSON.parse(lines.join('\n')) as Record<string, unknown> });
      } catch (error) {
        if (error instanceof SyntaxError)
          throw new ApiError(
            'The server sent an invalid stream event.',
            'INVALID_STREAM',
            undefined,
            true,
          );
        throw error;
      }
    }
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let match: RegExpExecArray | null;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        dispatch(block);
      }
      if (done) {
        if (buffer.trim()) dispatch(buffer);
        break;
      }
    }
  } finally {
    reader.releaseLock();
  }
}
export async function streamChat(
  payload: { conversation_id: string; message: string; retry_message_id?: string },
  signal: AbortSignal,
  onEvent: (event: StreamEvent) => void,
) {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    credentials: 'include',
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  await readEventStream(response, onEvent);
}
