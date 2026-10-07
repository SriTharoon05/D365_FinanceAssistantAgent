import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Copy,
  Check,
  RotateCcw,
  ThumbsDown,
  ThumbsUp,
  Volume2,
  ShieldCheck,
  Sparkles,
  AlertCircle,
} from 'lucide-react';
import type { EvidenceRecord, Message } from '../types';
import { endpoints } from '../lib/api';
import { formatDate } from '../lib/format';
import { ActionCard } from './ActionCard';
import { EvidenceCards } from './Evidence';
import { FinanceCharts } from './FinanceCharts';
interface Props {
  message: Message;
  conversationId: string;
  onEvidence: (records: EvidenceRecord[]) => void;
  onRetry: (id: string) => void;
  onSpeak: (content: string) => void;
  canSpeak: boolean;
  streaming: boolean;
  onUpdated: () => void;
  onError: (message: string) => void;
}
export function MessageView({
  message,
  conversationId,
  onEvidence,
  onRetry,
  onSpeak,
  canSpeak,
  streaming,
  onUpdated,
  onError,
}: Props) {
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<1 | -1 | null>(null);
  const [feedbackBusy, setFeedbackBusy] = useState(false);
  const assistant = message.role === 'assistant';
  const evidence = message.metadata?.evidence || [];
  const actions = message.metadata?.pending_actions || [];
  const hasChartData = evidence.some(
    (record) =>
      record.remaining_amount != null ||
      (record.kind === 'payment' && (record.amount != null || record.original_amount != null)),
  );
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      onError('Copy is unavailable in this browser. Select the text and copy it manually.');
    }
  };
  const rate = async (value: 1 | -1) => {
    setFeedbackBusy(true);
    try {
      await endpoints.feedback(message.id, value);
      setFeedback(value);
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Feedback could not be saved.');
    } finally {
      setFeedbackBusy(false);
    }
  };
  return (
    <article
      className={`message ${assistant ? 'assistant-message' : 'user-message'}`}
      aria-label={assistant ? 'Finance Assistant message' : 'Your message'}
    >
      <div className="message-avatar" aria-hidden="true">
        {assistant ? <Sparkles size={17} /> : 'You'}
      </div>
      <div className="message-body">
        <div className="message-author">
          <strong>{assistant ? 'Finance Assistant' : 'You'}</strong>
          <span>{formatDate(message.created_at, true)}</span>
          {assistant && message.model && <span className="message-model">{message.model}</span>}
        </div>
        <div className="message-bubble">
          {message.status === 'error' ? (
            <div className="message-error" role="alert">
              <AlertCircle size={18} />
              <div>
                <strong>Unable to complete this response</strong>
                <p>
                  {message.content ||
                    'The assistant is currently unavailable. Your message has been saved; retry when the service is ready.'}
                </p>
                {message.error_code && <small>{message.error_code}</small>}
              </div>
            </div>
          ) : (
            <div className={`markdown ${streaming ? 'is-streaming' : ''}`}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                skipHtml
                components={{
                  a: ({ href, children }) => (
                    <a href={href} target="_blank" rel="noopener noreferrer">
                      {children}
                    </a>
                  ),
                  table: ({ children }) => (
                    <div className="table-container">
                      <table>{children}</table>
                    </div>
                  ),
                }}
              >
                {message.content || (streaming ? ' ' : '')}
              </ReactMarkdown>
            </div>
          )}
          {message.status === 'interrupted' && (
            <p className="muted text-sm">Generation stopped. This response may be incomplete.</p>
          )}
        </div>
        {assistant &&
          !streaming &&
          message.status === 'completed' &&
          !actions.length &&
          hasChartData && <FinanceCharts messageId={message.id} />}
        {evidence.length > 0 && (
          <EvidenceCards records={evidence} onOpen={() => onEvidence(evidence)} />
        )}
        {actions.map((action) => (
          <ActionCard
            key={action.id}
            action={action}
            conversationId={conversationId}
            onUpdated={onUpdated}
            onError={onError}
          />
        ))}
        {!streaming && (
          <div className="message-actions">
            <button
              className="icon-button"
              onClick={() => void copy()}
              aria-label="Copy message"
              title="Copy message"
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
            {assistant && (
              <>
                <button
                  className="icon-button"
                  onClick={() => onRetry(message.id)}
                  aria-label="Regenerate response"
                  title="Regenerate response"
                >
                  <RotateCcw size={14} />
                </button>
                <button
                  className="icon-button"
                  onClick={() => onSpeak(message.content)}
                  disabled={!canSpeak}
                  aria-label="Speak response"
                  title={canSpeak ? 'Speak response' : 'Browser speech is unavailable or disabled'}
                >
                  <Volume2 size={14} />
                </button>
                <span className="action-separator" />
                <button
                  className={`icon-button ${feedback === 1 ? 'selected' : ''}`}
                  onClick={() => void rate(1)}
                  aria-label="Helpful response"
                  aria-pressed={feedback === 1}
                  disabled={feedbackBusy}
                >
                  <ThumbsUp size={14} />
                </button>
                <button
                  className={`icon-button ${feedback === -1 ? 'selected' : ''}`}
                  onClick={() => void rate(-1)}
                  aria-label="Unhelpful response"
                  aria-pressed={feedback === -1}
                  disabled={feedbackBusy}
                >
                  <ThumbsDown size={14} />
                </button>
                {evidence.length > 0 && (
                  <button className="text-button" onClick={() => onEvidence(evidence)}>
                    <ShieldCheck size={13} />
                    View evidence
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
