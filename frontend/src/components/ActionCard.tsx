import { useEffect, useState } from 'react';
import { CheckCircle2, ShieldCheck, XCircle, Clock3, AlertTriangle } from 'lucide-react';
import { endpoints } from '../lib/api';
import { displayValue, formatDate, label } from '../lib/format';
import type { PendingAction } from '../types';
import { Spinner } from './ui';
interface Props {
  action: PendingAction;
  conversationId: string;
  onUpdated: () => void;
  onError: (message: string) => void;
}
export function ActionCard({ action, conversationId, onUpdated, onError }: Props) {
  const [current, setCurrent] = useState(action);
  const [busy, setBusy] = useState<'confirm' | 'cancel' | null>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    setCurrent(action);
  }, [action]);
  useEffect(() => {
    if (!['pending', 'awaiting_confirmation'].includes(current.status)) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [current.status]);
  const expired =
    current.status === 'expired' ||
    (['pending', 'awaiting_confirmation'].includes(current.status) &&
      new Date(current.expires_at).valueOf() <= now);
  const pending = ['pending', 'awaiting_confirmation'].includes(current.status) && !expired;
  const choose = async (choice: 'confirm' | 'cancel') => {
    setBusy(choice);
    try {
      const result = await endpoints.action(current.id, choice, conversationId);
      setCurrent((previous) => ({ ...previous, ...result }));
      onUpdated();
    } catch (error) {
      onError(
        error instanceof Error
          ? error.message
          : 'The action could not be completed. Check the audit history before retrying.',
      );
      onUpdated();
    } finally {
      setBusy(null);
    }
  };
  const successful = ['executed', 'completed', 'confirmed', 'succeeded'].includes(current.status);
  const rawResult = current.result?.result;
  const result =
    rawResult && typeof rawResult === 'object'
      ? (rawResult as Record<string, unknown>)
      : current.result;
  const reference =
    result?.identifier || result?.journal_number || result?.reference || result?.external_id;
  const instructions = result?.manual_instructions;
  return (
    <section
      className={`action-card ${pending ? 'pending' : ''}`}
      aria-label="Financial action confirmation"
    >
      <div className="action-heading">
        <span className="action-icon">
          {successful ? (
            <CheckCircle2 size={19} />
          ) : expired ? (
            <Clock3 size={19} />
          ) : current.status === 'cancelled' ? (
            <XCircle size={19} />
          ) : (
            <ShieldCheck size={19} />
          )}
        </span>
        <div>
          <span className="eyebrow">
            {pending
              ? 'Your confirmation is required'
              : expired
                ? 'Confirmation expired'
                : label(current.status)}
          </span>
          <h3>{current.title}</h3>
        </div>
        <span className={`risk-label ${current.risk_level}`}>{label(current.risk_level)} risk</span>
      </div>
      <p>{current.description}</p>
      <dl className="action-fields">
        <div>
          <dt>Entity</dt>
          <dd>{current.entity}</dd>
        </div>
        {current.target_identifier && (
          <div>
            <dt>Record</dt>
            <dd>{current.target_identifier}</dd>
          </div>
        )}
        {Object.entries(current.proposed_changes || {}).map(([key, value]) => (
          <div key={key}>
            <dt>{label(key)}</dt>
            <dd>{displayValue(value)}</dd>
          </div>
        ))}
      </dl>
      {current.financial_impact && (
        <div className="action-impact">
          <AlertTriangle size={14} />
          <span>Financial impact: {displayValue(current.financial_impact)}</span>
        </div>
      )}
      {successful && Boolean(reference || instructions) && (
        <div className="action-verified-result">
          {Boolean(reference) && (
            <p>
              <strong>Reference:</strong> {displayValue(reference)}
            </p>
          )}
          {Boolean(instructions) && (
            <p>
              {Array.isArray(instructions)
                ? instructions.map(displayValue).join(' ')
                : displayValue(instructions)}
            </p>
          )}
          {result?.posted === false && <small>This record is unposted.</small>}
        </div>
      )}
      {pending ? (
        <>
          <div className="action-controls">
            <button className="button" onClick={() => void choose('confirm')} disabled={!!busy}>
              {busy === 'confirm' ? (
                <Spinner label="Executing…" />
              ) : (
                <>
                  <ShieldCheck size={15} />
                  Confirm action
                </>
              )}
            </button>
            <button
              className="button secondary"
              onClick={() => void choose('cancel')}
              disabled={!!busy}
            >
              {busy === 'cancel' ? 'Cancelling…' : 'Cancel'}
            </button>
          </div>
          <small>
            Expires {formatDate(current.expires_at, true)}. No change is made before confirmation.
          </small>
        </>
      ) : (
        <p className="action-result">
          {expired
            ? 'Ask the assistant to prepare a new action after reviewing current ERP data.'
            : successful
              ? 'The confirmed result is recorded in the audit history.'
              : current.status === 'cancelled'
                ? 'Cancelled. No ERP change was made.'
                : 'Review the audit history for the outcome before preparing another action.'}
        </p>
      )}
    </section>
  );
}
