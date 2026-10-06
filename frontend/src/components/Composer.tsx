import { useRef } from 'react';
import { ArrowUp, Mic, Square, X, LoaderCircle, AudioLines, Sparkles } from 'lucide-react';
import type { Capabilities } from '../types';
import { useRecorder } from '../hooks/useRecorder';
export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  disabled,
  capabilities,
  autoSendTranscript,
  onError,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: (message: string) => void;
  onStop: () => void;
  streaming: boolean;
  disabled: boolean;
  capabilities?: Capabilities;
  autoSendTranscript: boolean;
  onError: (message: string) => void;
}) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const recorder = useRecorder(
    (text) => {
      if (autoSendTranscript && !streaming) onSend(text);
      else {
        onChange(text);
        textarea.current?.focus();
      }
    },
    onError,
    capabilities?.voice_max_duration_seconds || 120,
    capabilities?.voice_max_upload_mb || 20,
  );
  const voiceReady = capabilities?.voice_input ?? false;
  const send = () => {
    if (value.trim() && !streaming && !disabled) onSend(value.trim());
  };
  return (
    <div className="composer-wrap">
      <div className={`composer ${recorder.state === 'recording' ? 'recording' : ''}`}>
        <div className="composer-input">
          {recorder.state === 'idle' ? (
            <>
              <Sparkles className="composer-sparkle" size={20} aria-hidden="true" />
              <textarea
                ref={textarea}
                aria-label="Message Finance Assistant"
                placeholder="Ask a question or describe a finance task…"
                value={value}
                onChange={(event) => {
                  onChange(event.target.value);
                  event.target.style.height = 'auto';
                  event.target.style.height = `${Math.min(event.target.scrollHeight, 160)}px`;
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    send();
                  }
                }}
                rows={2}
                disabled={disabled}
              />
            </>
          ) : (
            <div className="recording-preview" role="status" aria-live="polite">
              {recorder.state === 'recording' ? (
                <>
                  <span className="recording-dot" />
                  <AudioLines className="pulse" size={26} />
                  <strong>Recording</strong>
                  <span>
                    {Math.floor(recorder.seconds / 60)}:
                    {String(recorder.seconds % 60).padStart(2, '0')}
                  </span>
                </>
              ) : (
                <>
                  <LoaderCircle size={20} className="animate-spin" />
                  <strong>Transcribing your recording…</strong>
                </>
              )}
            </div>
          )}
        </div>
        <div className="composer-bottom">
          <div className="composer-context">
            <span className="company-mini">{(capabilities?.company || 'USMF').toUpperCase()}</span>
            <span className="composer-service">Dynamics 365 Finance</span>
          </div>
          <div className="composer-controls">
            {recorder.state !== 'idle' ? (
              <>
                <button
                  className="icon-button"
                  onClick={recorder.cancel}
                  aria-label="Cancel recording"
                  title="Discard recording"
                >
                  <X size={17} />
                </button>
                {recorder.state === 'recording' && (
                  <button
                    className="record-stop"
                    onClick={recorder.stop}
                    aria-label="Stop recording and transcribe"
                  >
                    <Square size={13} />
                    Transcribe
                  </button>
                )}
              </>
            ) : (
              <button
                className="icon-button mic-button"
                onClick={() => void recorder.start()}
                disabled={!voiceReady || streaming || disabled}
                aria-label={
                  voiceReady ? 'Record voice message' : 'Voice transcription is not configured'
                }
                title={
                  voiceReady
                    ? 'Record a voice message'
                    : 'Voice transcription is not configured. Add GROQ_API_KEY on the server.'
                }
              >
                <Mic size={19} />
              </button>
            )}
            {streaming ? (
              <button
                className="send-button stop-generation"
                onClick={onStop}
                aria-label="Stop generation"
                title="Stop generation"
              >
                <Square size={15} fill="currentColor" />
              </button>
            ) : (
              <button
                className="send-button"
                onClick={send}
                aria-label="Send message"
                title="Send message"
                disabled={!value.trim() || disabled || recorder.state !== 'idle'}
              >
                <ArrowUp size={20} />
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="composer-caption">
        <span>Verify financial decisions against source records.</span>
        <span>Enter to send · Shift + Enter for a new line</span>
      </div>
    </div>
  );
}
