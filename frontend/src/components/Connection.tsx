import { AlertTriangle, CircleCheck, RefreshCw, WifiOff } from 'lucide-react';
import type { IntegrationStatus } from '../types';
import { connectionProgress, connectionTiming } from '../lib/integration';
export function ConnectionBadge({
  status,
  onClick,
}: {
  status?: IntegrationStatus;
  onClick?: () => void;
}) {
  const current = status?.status || 'connecting';
  const pending = ['connecting', 'reconnecting'].includes(current);
  return (
    <button
      className={`connection-badge ${current}`}
      onClick={onClick}
      aria-label={`Dynamics 365 ${current}. Open integration settings`}
    >
      <span className={`connection-dot ${pending ? 'pulse' : ''}`} />
      <span className="connection-label">
        Dynamics 365 <strong>{current.charAt(0).toUpperCase() + current.slice(1)}</strong>
      </span>
    </button>
  );
}
export function ConnectionBanner({
  status,
  onReconnect,
  isReconnecting,
}: {
  status?: IntegrationStatus;
  onReconnect: () => void;
  isReconnecting: boolean;
}) {
  if (!status || (status.status === 'connected' && !isReconnecting)) return null;
  const reconnecting = isReconnecting || status.status === 'reconnecting';
  const isBusy = reconnecting || status.status === 'connecting';
  return (
    <div className={`connection-banner ${status.status}`} role="status">
      {isBusy ? (
        <RefreshCw className="animate-spin shrink-0" size={18} />
      ) : status.status === 'degraded' ? (
        <AlertTriangle className="shrink-0" size={18} />
      ) : (
        <WifiOff className="shrink-0" size={18} />
      )}
      <div>
        <strong>
          {reconnecting
            ? 'Reconnecting to Dynamics 365'
            : isBusy
              ? 'Connecting to Dynamics 365'
              : status.status === 'degraded'
                ? 'Some finance capabilities are unavailable'
                : 'Dynamics 365 is disconnected'}
        </strong>
        <p>
          {status.last_error_summary ||
            (isBusy
              ? `${connectionProgress(status)} Your saved conversations remain accessible.`
              : 'Live finance data is unavailable. Your saved conversations remain accessible.')}
        </p>
        {isBusy && connectionTiming(status) && (
          <small className="connection-progress">{connectionTiming(status)}</small>
        )}
      </div>
      <button className="button small secondary" onClick={onReconnect} disabled={isBusy}>
        <RefreshCw size={14} />
        {reconnecting ? 'Reconnecting…' : isBusy ? 'Connecting…' : 'Reconnect'}
      </button>
    </div>
  );
}
export function TrustNote() {
  return (
    <span className="trust-note">
      <CircleCheck size={13} />
      Answers grounded in ERP evidence
    </span>
  );
}
