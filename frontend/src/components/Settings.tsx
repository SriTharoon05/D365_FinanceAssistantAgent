import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Monitor,
  Moon,
  Sun,
  RefreshCw,
  ShieldCheck,
  Download,
  Trash2,
  Volume2,
  Settings2,
  Cpu,
  Database,
  PlugZap,
  Clock3,
} from 'lucide-react';
import type { Capabilities, IntegrationStatus, Preferences } from '../types';
import { endpoints, exportConversation } from '../lib/api';
import { displayValue, formatDate, label } from '../lib/format';
import { Modal, Spinner } from './ui';
import { ConnectionBadge } from './Connection';
interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preferences: Preferences;
  updatePreferences: (changes: Partial<Preferences>) => void;
  status?: IntegrationStatus;
  capabilities?: Capabilities;
  onReconnect: () => void;
  reconnecting: boolean;
  voices: SpeechSynthesisVoice[];
  speechSupported: boolean;
  conversationId?: string;
  sessionReady?: boolean;
  onError: (message: string) => void;
  onSuccess: (message: string) => void;
}
function Toggle({
  checked,
  onChange,
  label: name,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={name}
      className={`toggle ${checked ? 'checked' : ''}`}
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
}
export function Settings({
  open,
  onOpenChange,
  preferences,
  updatePreferences,
  status,
  capabilities,
  onReconnect,
  reconnecting,
  voices,
  speechSupported,
  conversationId,
  onError,
  onSuccess,
  sessionReady = true,
}: Props) {
  const [tab, setTab] = useState('general');
  const [clearConfirm, setClearConfirm] = useState(false);
  const [clearing, setClearing] = useState(false);
  const audit = useQuery({
    queryKey: ['audit'],
    queryFn: endpoints.audit,
    enabled: open && tab === 'audit' && sessionReady,
  });
  const clear = async () => {
    setClearing(true);
    try {
      await endpoints.clearArchived();
      onSuccess('Archived conversations were cleared.');
      setClearConfirm(false);
    } catch (error) {
      onError(
        error instanceof Error ? error.message : 'Archived conversations could not be cleared.',
      );
    } finally {
      setClearing(false);
    }
  };
  const exportCurrent = async (format: 'markdown' | 'json') => {
    if (!conversationId) return;
    try {
      await exportConversation(conversationId, format);
      onSuccess('Conversation exported.');
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Export failed.');
    }
  };
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="Workspace settings"
      description="Appearance, connected services, and local data preferences."
      className="settings-dialog"
    >
      <div className="settings-layout">
        <nav className="settings-tabs" aria-label="Settings sections">
          {[
            { id: 'general', name: 'Appearance', Icon: Settings2 },
            { id: 'voice', name: 'Voice', Icon: Volume2 },
            { id: 'integration', name: 'Dynamics 365', Icon: PlugZap },
            { id: 'ai', name: 'AI & capabilities', Icon: Cpu },
            { id: 'privacy', name: 'Local data', Icon: Database },
            { id: 'audit', name: 'Audit history', Icon: ShieldCheck },
          ].map(({ id, name, Icon }) => (
            <button
              key={id}
              className={tab === id ? 'active' : ''}
              onClick={() => setTab(id)}
              aria-current={tab === id ? 'page' : undefined}
            >
              <Icon size={16} />
              {name}
            </button>
          ))}
        </nav>
        <div className="settings-body">
          {tab === 'general' && (
            <>
              <h3>Make the workspace yours</h3>
              <p className="muted">A comfortable space for your daily finance work.</p>
              <h4>Appearance</h4>
              <div className="theme-options">
                {[
                  { value: 'light', Icon: Sun },
                  { value: 'dark', Icon: Moon },
                  { value: 'system', Icon: Monitor },
                ].map(({ value, Icon }) => (
                  <button
                    key={value}
                    className={preferences.theme === value ? 'selected' : ''}
                    onClick={() => updatePreferences({ theme: value as Preferences['theme'] })}
                    aria-pressed={preferences.theme === value}
                  >
                    <Icon size={21} />
                    {label(value)}
                  </button>
                ))}
              </div>
              <p className="setting-note">
                Display and voice preferences are stored in this browser. Your conversations are
                stored in the application database.
              </p>
            </>
          )}
          {tab === 'voice' && (
            <>
              <h3>Voice preferences</h3>
              <p className="muted">Browser speech output. Optional Groq transcription for input.</p>
              <div className="setting-row">
                <div>
                  <strong>Speech output</strong>
                  <span>
                    {speechSupported
                      ? 'Listen to assistant responses using browser speech.'
                      : 'This browser does not support speech synthesis.'}
                  </span>
                </div>
                <Toggle
                  checked={preferences.speechEnabled}
                  onChange={(speechEnabled) => updatePreferences({ speechEnabled })}
                  label="Speech output"
                />
              </div>
              <div className="setting-row">
                <div>
                  <strong>Auto-read responses</strong>
                  <span>Read a completed response automatically.</span>
                </div>
                <Toggle
                  checked={preferences.autoRead}
                  onChange={(autoRead) => updatePreferences({ autoRead })}
                  label="Auto-read responses"
                />
              </div>
              <label className="field-label" htmlFor="browser-voice">
                Browser voice
              </label>
              <select
                id="browser-voice"
                value={preferences.speechVoice}
                onChange={(event) => updatePreferences({ speechVoice: event.target.value })}
              >
                <option value="">Browser default</option>
                {voices.map((voice) => (
                  <option key={voice.voiceURI} value={voice.voiceURI}>
                    {voice.name} ({voice.lang})
                  </option>
                ))}
              </select>
              <label className="field-label" htmlFor="speech-rate">
                Speech rate <span>{preferences.speechRate.toFixed(1)}×</span>
              </label>
              <input
                id="speech-rate"
                type="range"
                min="0.5"
                max="1.5"
                step="0.1"
                value={preferences.speechRate}
                onChange={(event) => updatePreferences({ speechRate: Number(event.target.value) })}
              />
              <div className="setting-row">
                <div>
                  <strong>Auto-send transcript</strong>
                  <span>Send transcribed audio without reviewing it in the composer.</span>
                </div>
                <Toggle
                  checked={preferences.autoSendTranscript}
                  onChange={(autoSendTranscript) => updatePreferences({ autoSendTranscript })}
                  label="Auto-send transcript"
                />
              </div>
              <div className="settings-callout">
                {capabilities?.voice_input
                  ? `Voice input is available · ${capabilities.voice_model}`
                  : 'Voice transcription is not configured. Add GROQ_API_KEY to the backend .env file. Text chat remains available.'}
              </div>
            </>
          )}
          {tab === 'integration' && (
            <>
              <h3>Dynamics 365 Finance</h3>
              <div className="settings-integration">
                <ConnectionBadge status={status} />
                <span className="company-pill">
                  {(status?.company || capabilities?.company || 'USMF').toUpperCase()}
                </span>
              </div>
              {status?.mock_mode && (
                <div className="settings-callout">
                  Mock ERP mode is enabled. All finance records are demonstration data.
                </div>
              )}
              <dl className="settings-diagnostics">
                <div>
                  <dt>Last successful request</dt>
                  <dd>{formatDate(status?.last_success_at, true)}</dd>
                </div>
                <div>
                  <dt>OData metadata</dt>
                  <dd>{status?.metadata_loaded ? 'Loaded' : 'Not loaded'}</dd>
                </div>
                <div>
                  <dt>Response latency</dt>
                  <dd>
                    {status?.latency_ms !== null && status?.latency_ms !== undefined
                      ? `${Math.round(status.latency_ms)} ms`
                      : 'Not available'}
                  </dd>
                </div>
              </dl>
              {status?.last_error_summary && (
                <p className="settings-callout warning">{status.last_error_summary}</p>
              )}
              <button className="button secondary" onClick={onReconnect} disabled={reconnecting}>
                {reconnecting ? (
                  <Spinner label="Reconnecting…" />
                ) : (
                  <>
                    <RefreshCw size={15} />
                    Reconnect Dynamics 365
                  </>
                )}
              </button>
              <h4>Resolved capabilities</h4>
              <div className="capability-list">
                {Object.entries(status?.capabilities || {}).length === 0 ? (
                  <p className="muted">
                    Capabilities are detected after authentication and metadata validation.
                  </p>
                ) : (
                  Object.entries(status?.capabilities || {}).map(([key, value]) => (
                    <div key={key}>
                      <span>{label(key)}</span>
                      <strong className={value ? 'available' : 'unavailable'}>
                        {typeof value === 'boolean'
                          ? value
                            ? 'Available'
                            : 'Unavailable'
                          : value
                            ? displayValue(value)
                            : 'Not resolved'}
                      </strong>
                    </div>
                  ))
                )}
              </div>
              <p className="setting-note">
                Missing transaction entities can be configured on the backend. No connection secrets
                are sent to this browser.
              </p>
            </>
          )}
          {tab === 'ai' && (
            <>
              <h3>AI & connected capabilities</h3>
              <div className="model-card">
                <Cpu size={28} />
                <div>
                  <strong>{capabilities?.model || 'gpt-4.1-mini'}</strong>
                  <span>
                    {capabilities?.ai_configured
                      ? 'Azure OpenAI configured'
                      : capabilities?.mock_mode
                        ? 'Deterministic mock assistant · no live LLM'
                        : 'Azure OpenAI is not configured'}
                  </span>
                </div>
              </div>
              <dl className="settings-diagnostics">
                <div>
                  <dt>Voice transcription</dt>
                  <dd>{capabilities?.voice_input ? capabilities.voice_model : 'Not configured'}</dd>
                </div>
                <div>
                  <dt>ERP mutations</dt>
                  <dd>
                    {capabilities?.write_actions_enabled ? 'Enabled with confirmation' : 'Disabled'}
                  </dd>
                </div>
                <div>
                  <dt>Data mode</dt>
                  <dd>
                    {capabilities?.mock_mode ? 'Mock demonstration data' : 'Live Dynamics 365'}
                  </dd>
                </div>
              </dl>
              <p className="settings-callout">
                Financial amounts are calculated by deterministic backend tools. Writes require an
                explicit confirmation and are recorded in the audit history.
              </p>
            </>
          )}
          {tab === 'privacy' && (
            <>
              <h3>Your local conversation data</h3>
              <p className="muted">
                Chat history, evidence, and audit records persist in the application database across
                browser refreshes and server restarts. Audio recordings are not stored permanently.
              </p>
              <h4>Export this conversation</h4>
              <div className="flex gap-2 flex-wrap">
                <button
                  className="button secondary small"
                  onClick={() => void exportCurrent('markdown')}
                  disabled={!conversationId}
                >
                  <Download size={14} />
                  Markdown
                </button>
                <button
                  className="button secondary small"
                  onClick={() => void exportCurrent('json')}
                  disabled={!conversationId}
                >
                  <Download size={14} />
                  JSON
                </button>
              </div>
              <h4>Archived conversations</h4>
              <p className="muted">
                Remove archived conversations from this workspace. Local records are retained for
                auditing.
              </p>
              {clearConfirm ? (
                <div className="settings-callout warning">
                  <strong>Remove all archived conversations?</strong>
                  <p>
                    They will no longer appear in your history. Pending confirmations will be
                    cancelled.
                  </p>
                  <div className="flex gap-2">
                    <button
                      className="button danger small"
                      onClick={() => void clear()}
                      disabled={clearing}
                    >
                      {clearing ? 'Clearing…' : 'Remove archived chats'}
                    </button>
                    <button
                      className="button secondary small"
                      onClick={() => setClearConfirm(false)}
                    >
                      Keep conversations
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  className="button secondary small danger-text"
                  onClick={() => setClearConfirm(true)}
                >
                  <Trash2 size={14} />
                  Clear archived conversations
                </button>
              )}
              <p className="setting-note">
                Backend credentials are configured on the server. This settings screen never
                displays or edits secrets.
              </p>
            </>
          )}
          {tab === 'audit' && (
            <>
              <h3>Recent ERP audit history</h3>
              <p className="muted">
                Confirmed mutation outcomes, including errors and operations that need verification.
              </p>
              {audit.isLoading ? (
                <Spinner label="Loading audit history…" />
              ) : audit.isError ? (
                <div className="settings-callout warning">
                  {audit.error.message}
                  <button className="text-button" onClick={() => void audit.refetch()}>
                    Retry
                  </button>
                </div>
              ) : !audit.data?.length ? (
                <div className="audit-empty">
                  <ShieldCheck size={28} />
                  <strong>No mutations recorded</strong>
                  <p>Confirmed ERP actions will appear here.</p>
                </div>
              ) : (
                <div className="audit-list">
                  {audit.data.map((event) => (
                    <article key={event.id}>
                      <div>
                        <strong>{label(event.action || event.action_type || 'ERP action')}</strong>
                        <span className="audit-status">
                          {label(event.result_status || event.status || 'Recorded')}
                        </span>
                      </div>
                      <p>
                        {event.entity} {event.record_identifier} ·{' '}
                        {event.company?.toUpperCase() || 'ERP'}
                      </p>
                      <small>
                        <Clock3 size={11} />
                        {formatDate(event.created_at || event.timestamp, true)}
                      </small>
                    </article>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </Modal>
  );
}
