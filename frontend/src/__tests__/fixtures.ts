import type {
  Capabilities,
  Conversation,
  EvidenceRecord,
  IntegrationStatus,
  PendingAction,
} from '../types';

export const connectedStatus: IntegrationStatus = {
  status: 'connected',
  company: 'USMF',
  last_success_at: '2026-10-05T08:00:00Z',
  last_error_summary: null,
  metadata_loaded: true,
  capabilities: { customers: true, open_transactions: true },
  latency_ms: 42,
  mock_mode: false,
};

export const unavailableStatus: IntegrationStatus = {
  ...connectedStatus,
  status: 'disconnected',
  last_error_summary: 'The Dynamics 365 access token could not be refreshed.',
  metadata_loaded: false,
};

export const capabilities: Capabilities = {
  model: 'gpt-4.1-mini',
  ai_configured: true,
  voice_input: false,
  voice_model: 'whisper-large-v3-turbo',
  mock_mode: false,
  company: 'USMF',
  write_actions_enabled: true,
};

export function conversation(id: string, title: string): Conversation {
  return {
    id,
    title,
    selected_company: 'USMF',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
}

export const pendingAction: PendingAction = {
  id: 'action-1',
  action_type: 'create_customer_payment_journal',
  title: 'Create customer payment journal for DEMO-001?',
  description: 'Create an unposted journal. Posting will remain a separate operation.',
  entity: 'CustomerPaymentJournalHeaders',
  target_identifier: 'DEMO-001',
  proposed_changes: {
    account: 'DEMO-001',
    amount: '123.45',
    currency: 'EUR',
    journal_name: 'CustPay',
  },
  financial_impact: 'EUR 123.45 unposted payment',
  risk_level: 'high',
  expires_at: new Date(Date.now() + 600_000).toISOString(),
  status: 'pending',
};

export const invoiceEvidence: EvidenceRecord = {
  kind: 'open_transaction',
  company: 'USMF',
  currency: 'EUR',
  account: 'DEMO-001',
  customer_name: 'Example Services Ltd',
  invoice_number: 'INV-42',
  original_amount: '200.00',
  remaining_amount: '123.45',
  due_date: '2026-09-30',
  voucher: 'VCH-42',
  source_entity: 'VerifiedCustomerOpenTransactions',
  retrieved_at: '2026-10-05T08:00:00Z',
};
