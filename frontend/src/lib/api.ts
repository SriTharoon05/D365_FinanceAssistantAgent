import type {
  AuditEvent,
  Capabilities,
  ChartResponse,
  Conversation,
  IntegrationStatus,
  Message,
  PendingAction,
} from '../types';

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/$/, '');
export class ApiError extends Error {
  constructor(
    message: string,
    public code = 'NETWORK_ERROR',
    public requestId?: string,
    public retryable = false,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
export async function responseError(response: Response): Promise<ApiError> {
  try {
    const payload = await response.json();
    const error = payload.error || payload;
    return new ApiError(
      error.message || 'The server could not complete this request.',
      error.code || `HTTP_${response.status}`,
      error.request_id,
      error.retryable ?? response.status >= 500,
    );
  } catch {
    return new ApiError(
      `The server returned HTTP ${response.status}. Please try again.`,
      `HTTP_${response.status}`,
      undefined,
      response.status >= 500,
    );
  }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      credentials: 'include',
      ...options,
      headers: {
        ...(options.body && !(options.body instanceof FormData)
          ? { 'Content-Type': 'application/json' }
          : {}),
        ...options.headers,
      },
    });
  } catch {
    throw new ApiError(
      'The Finance Assistant server is unreachable. Check that the backend is running.',
      'NETWORK_ERROR',
      undefined,
      true,
    );
  }
  if (!response.ok) throw await responseError(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const endpoints = {
  capabilities: () => api<Capabilities>('/settings/capabilities'),
  status: () => api<IntegrationStatus>('/integrations/d365/status'),
  reconnect: () => api<IntegrationStatus>('/integrations/d365/reconnect', { method: 'POST' }),
  conversations: (q = '', archived = false) =>
    api<Conversation[]>(`/conversations?q=${encodeURIComponent(q)}&archived=${archived}`),
  conversation: (id: string) => api<Conversation>(`/conversations/${encodeURIComponent(id)}`),
  createConversation: (selected_company: string) =>
    api<Conversation>('/conversations', {
      method: 'POST',
      body: JSON.stringify({ selected_company }),
    }),
  messages: (id: string) => api<Message[]>(`/conversations/${encodeURIComponent(id)}/messages`),
  charts: (id: string) => api<ChartResponse>(`/messages/${encodeURIComponent(id)}/charts`),
  updateConversation: (id: string, changes: { title?: string; archived?: boolean }) =>
    api<Conversation>(`/conversations/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(changes),
    }),
  deleteConversation: (id: string) =>
    api<void>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  clearArchived: () => api<void>('/conversations?archived=true', { method: 'DELETE' }),
  action: (id: string, choice: 'confirm' | 'cancel' | 'verify', conversation_id: string) =>
    api<Pick<PendingAction, 'id' | 'status' | 'result'>>(
      `/actions/${encodeURIComponent(id)}/${choice}`,
      {
        method: 'POST',
        body: JSON.stringify({ conversation_id }),
      },
    ),
  feedback: (id: string, value: 1 | -1) =>
    api<void>(`/messages/${encodeURIComponent(id)}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ value }),
    }),
  audit: () => api<AuditEvent[]>('/audit/recent'),
  transcribe: (file: Blob, signal?: AbortSignal, durationSeconds?: number) => {
    const data = new FormData();
    data.append(
      'file',
      file,
      `recording.${file.type.includes('mp4') ? 'm4a' : file.type.includes('ogg') ? 'ogg' : 'webm'}`,
    );
    if (durationSeconds !== undefined) data.append('duration_seconds', String(durationSeconds));
    return api<{ text: string }>('/voice/transcribe', { method: 'POST', body: data, signal });
  },
};
export function chartImageUrl(
  messageId: string,
  chartId: string,
  theme: 'light' | 'dark',
  size: 'standard' | 'compact' = 'standard',
) {
  return `${API_BASE}/messages/${encodeURIComponent(messageId)}/charts/${encodeURIComponent(chartId)}.png?theme=${theme}${size === 'compact' ? '&size=compact' : ''}`;
}
export async function exportConversation(id: string, format: 'markdown' | 'json') {
  const response = await fetch(`${API_BASE}/conversations/${encodeURIComponent(id)}/export`, {
    credentials: 'include',
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ format }),
  });
  if (!response.ok) throw await responseError(response);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `finance-conversation-${id}.${format === 'markdown' ? 'md' : 'json'}`;
  anchor.click();
  URL.revokeObjectURL(url);
}
