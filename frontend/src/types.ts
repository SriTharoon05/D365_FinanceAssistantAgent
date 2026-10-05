export type ConnectionState =
  'connected' | 'connecting' | 'reconnecting' | 'degraded' | 'disconnected';
export interface IntegrationStatus {
  status: ConnectionState;
  company: string;
  last_success_at?: string | null;
  last_error_summary?: string | null;
  metadata_loaded: boolean;
  capabilities: Record<string, boolean | string | string[] | Record<string, string | null> | null>;
  latency_ms?: number | null;
  mock_mode: boolean;
}
export interface Capabilities {
  model: string;
  ai_configured: boolean;
  voice_input: boolean;
  voice_model: string;
  voice_max_duration_seconds?: number;
  voice_max_upload_mb?: number;
  mock_mode: boolean;
  company: string;
  write_actions_enabled: boolean;
}
export interface Conversation {
  id: string;
  title: string;
  selected_company: string;
  created_at: string;
  updated_at: string;
  archived_at?: string | null;
}
export interface EvidenceRecord {
  kind?: string;
  company?: string;
  currency?: string;
  account?: string;
  customer_name?: string;
  invoice_number?: string;
  external_invoice_id?: string;
  original_amount?: string | number;
  remaining_amount?: string | number;
  due_date?: string;
  voucher?: string;
  source_entity?: string;
  retrieved_at?: string;
  [key: string]: unknown;
}
export interface PendingAction {
  id: string;
  action_type: string;
  title: string;
  description: string;
  entity: string;
  target_identifier?: string;
  proposed_changes: Record<string, unknown>;
  financial_impact?: string | Record<string, unknown> | null;
  risk_level: string;
  expires_at: string;
  status: string;
  result?: Record<string, unknown> | null;
}
export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  status: string;
  model?: string | null;
  created_at: string;
  error_code?: string | null;
  metadata: {
    evidence?: EvidenceRecord[];
    pending_actions?: PendingAction[];
    [key: string]: unknown;
  };
}
export interface AuditEvent {
  id: string;
  action?: string;
  action_type?: string;
  created_at?: string;
  timestamp?: string;
  result_status?: string;
  status?: string;
  entity?: string;
  company?: string;
  record_identifier?: string;
}
export interface Preferences {
  theme: 'light' | 'dark' | 'system';
  speechEnabled: boolean;
  autoRead: boolean;
  speechVoice: string;
  speechRate: number;
  autoSendTranscript: boolean;
}
