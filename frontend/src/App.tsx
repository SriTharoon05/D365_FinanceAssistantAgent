import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Archive,
  ArrowLeft,
  ChevronDown,
  Cpu,
  FileSpreadsheet,
  Menu,
  Mic,
  Pause,
  Play,
  Search,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Square,
  X,
} from 'lucide-react';
import type { Conversation, EvidenceRecord, IntegrationStatus } from './types';
import { endpoints } from './lib/api';
import { integrationPollInterval } from './lib/integration';
import { usePreferences } from './hooks/usePreferences';
import { useSpeech } from './hooks/useSpeech';
import { useChatStream } from './hooks/useChatStream';
import { ConnectionBadge, ConnectionBanner, TrustNote } from './components/Connection';
import { ConversationList } from './components/ConversationList';
import { MessageView } from './components/MessageView';
import { EvidencePanel } from './components/Evidence';
import { Settings } from './components/Settings';
import { Composer } from './components/Composer';
import { Welcome } from './components/Welcome';
import { Modal, Spinner, Toast } from './components/ui';
export default function App() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [search, setSearch] = useState('');
  const [archived, setArchived] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [composer, setComposer] = useState('');
  const [evidence, setEvidence] = useState<EvidenceRecord[]>([]);
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [toast, setToast] = useState<{ message: string; kind: 'error' | 'success' } | null>(null);
  const [conversationAction, setConversationAction] = useState<{
    kind: 'rename' | 'delete';
    conversation: Conversation;
  } | null>(null);
  const [newTitle, setNewTitle] = useState('');
  const [actionBusy, setActionBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);
  const { preferences, updatePreferences } = usePreferences();
  const speech = useSpeech(preferences);
  const notifyError = useCallback((message: string) => setToast({ message, kind: 'error' }), []);
  const notifySuccess = useCallback(
    (message: string) => setToast({ message, kind: 'success' }),
    [],
  );
  const capabilities = useQuery({
    queryKey: ['capabilities'],
    queryFn: endpoints.capabilities,
    refetchOnWindowFocus: true,
  });
  const status = useQuery({
    queryKey: ['integration'],
    queryFn: endpoints.status,
    refetchInterval: (query) => integrationPollInterval(query.state.data?.status),
    refetchOnWindowFocus: true,
  });
  const bootstrap = useQuery({
    queryKey: ['session-bootstrap'],
    queryFn: () => endpoints.conversations(),
    staleTime: Infinity,
  });
  const conversations = useQuery({
    queryKey: ['conversations', search, archived],
    queryFn: () => endpoints.conversations(search, archived),
    enabled: bootstrap.isSuccess,
  });
  const current = useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () => endpoints.conversation(conversationId!),
    enabled: !!conversationId && bootstrap.isSuccess,
  });
  const messages = useQuery({
    queryKey: ['messages', conversationId],
    queryFn: () => endpoints.messages(conversationId!),
    enabled: !!conversationId && bootstrap.isSuccess,
  });
  const reconnect = useMutation({
    mutationFn: endpoints.reconnect,
    onMutate: () => {
      const previous = client.getQueryData<IntegrationStatus>(['integration']);
      if (previous) client.setQueryData(['integration'], { ...previous, status: 'reconnecting' });
    },
    onSuccess: (result) => {
      client.setQueryData(['integration'], result);
      void client.invalidateQueries({ queryKey: ['capabilities'] });
      if (result.status === 'connected') notifySuccess('Dynamics 365 connection verified.');
      else if (result.status === 'degraded')
        notifySuccess('Dynamics 365 connected with limited finance capabilities.');
      else if (result.status === 'disconnected')
        notifyError(
          result.last_error_summary ||
            'Dynamics 365 is not connected. Review the integration status for details.',
        );
    },
    onError: (error) => {
      notifyError(error.message);
      void status.refetch();
    },
  });
  const effectiveStatus: IntegrationStatus | undefined = status.isError
    ? {
        status: 'disconnected',
        company: capabilities.data?.company || 'usmf',
        metadata_loaded: false,
        capabilities: {},
        mock_mode: false,
        last_error_summary: status.error.message,
      }
    : status.data;
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), toast.kind === 'error' ? 10_000 : 4_000);
    return () => clearTimeout(timer);
  }, [toast]);
  const refresh = useCallback(
    async (id?: string) => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ['messages', id || conversationId] }),
        client.invalidateQueries({ queryKey: ['conversations'] }),
        client.invalidateQueries({ queryKey: ['conversation', id || conversationId] }),
        client.invalidateQueries({ queryKey: ['audit'] }),
      ]);
    },
    [client, conversationId],
  );
  const createChat = useCallback(async () => {
    if (creatingRef.current) return undefined;
    creatingRef.current = true;
    setCreating(true);
    try {
      await client.ensureQueryData({
        queryKey: ['session-bootstrap'],
        queryFn: () => endpoints.conversations(),
        staleTime: Infinity,
      });
      const conversation = await endpoints.createConversation(capabilities.data?.company || 'usmf');
      client.setQueryData(['conversation', conversation.id], conversation);
      client.setQueryData(['messages', conversation.id], []);
      await client.invalidateQueries({ queryKey: ['conversations'] });
      navigate(`/chat/${conversation.id}`);
      setSidebarOpen(false);
      setComposer('');
      setArchived(false);
      return conversation;
    } catch (error) {
      notifyError(
        error instanceof Error ? error.message : 'A new conversation could not be created.',
      );
      return undefined;
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  }, [capabilities.data?.company, client, navigate, notifyError]);
  const { generation, generating, send, retry, stopGeneration } = useChatStream({
    conversationId,
    model: capabilities.data?.model,
    preferences,
    speech,
    createChat,
    refresh,
    notifyError,
    setComposer,
  });
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: generation ? 'auto' : 'smooth', block: 'end' });
  }, [messages.data, generation]);

  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        if (!generating.current) void createChat();
      }
      if (event.key === 'Escape' && generating.current) stopGeneration();
    };
    window.addEventListener('keydown', shortcut);
    return () => window.removeEventListener('keydown', shortcut);
  }, [createChat, generating, stopGeneration]);
  const archiveConversation = async (conversation: Conversation) => {
    try {
      await endpoints.updateConversation(conversation.id, { archived: !conversation.archived_at });
      await client.invalidateQueries({ queryKey: ['conversations'] });
      await client.invalidateQueries({ queryKey: ['conversation', conversation.id] });
      notifySuccess(conversation.archived_at ? 'Conversation restored.' : 'Conversation archived.');
    } catch (error) {
      notifyError(
        error instanceof Error ? error.message : 'The conversation could not be archived.',
      );
    }
  };
  const completeConversationAction = async () => {
    if (!conversationAction || (conversationAction.kind === 'rename' && !newTitle.trim())) return;
    setActionBusy(true);
    try {
      const { kind, conversation } = conversationAction;
      if (kind === 'rename')
        await endpoints.updateConversation(conversation.id, { title: newTitle.trim() });
      else {
        await endpoints.deleteConversation(conversation.id);
        if (conversationId === conversation.id) navigate('/');
      }
      await client.invalidateQueries({ queryKey: ['conversations'] });
      await client.invalidateQueries({ queryKey: ['conversation', conversation.id] });
      setConversationAction(null);
      notifySuccess(kind === 'rename' ? 'Conversation renamed.' : 'Conversation deleted.');
    } catch (error) {
      notifyError(
        error instanceof Error ? error.message : 'The conversation could not be updated.',
      );
    } finally {
      setActionBusy(false);
    }
  };
  const openEvidence = (records: EvidenceRecord[]) => {
    setEvidence(records);
    setEvidenceOpen(true);
  };
  const selectedMessages = (messages.data || []).filter((message) => message.role !== 'system');
  const activeGeneration = generation?.conversationId === conversationId;
  return (
    <div className="app-shell">
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="brand">
          <div className="brand-symbol">
            <FileSpreadsheet size={23} />
          </div>
          <div>
            <strong>Finance Assistant</strong>
            <span>DYNAMICS 365 WORKSPACE</span>
          </div>
          <button
            className="mobile-close icon-button"
            aria-label="Close navigation"
            onClick={() => setSidebarOpen(false)}
          >
            <X size={18} />
          </button>
        </div>
        <ConversationList
          conversations={conversations.data || []}
          activeId={conversationId}
          onSelect={(id) => {
            navigate(`/chat/${id}`);
            setSidebarOpen(false);
            setComposer('');
          }}
          onNew={() => {
            if (!generating.current) void createChat();
          }}
          onRename={(conversation) => {
            setNewTitle(conversation.title);
            setConversationAction({ kind: 'rename', conversation });
          }}
          onArchive={(conversation) => void archiveConversation(conversation)}
          onDelete={(conversation) => setConversationAction({ kind: 'delete', conversation })}
          isLoading={bootstrap.isLoading || conversations.isLoading}
        />
        <div className="sidebar-search">
          <Search size={14} />
          <input
            type="search"
            placeholder="Search conversations"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            aria-label="Search conversations"
          />
        </div>
        <button
          className={`archived-button ${archived ? 'active' : ''}`}
          onClick={() => setArchived(!archived)}
        >
          <Archive size={14} />
          {archived ? 'Viewing archived chats' : 'Archived conversations'}
          {archived && <ArrowLeft size={13} />}
        </button>
        {(conversations.isError || bootstrap.isError) && (
          <div className="sidebar-error" role="alert">
            {(conversations.error || bootstrap.error)?.message}
            <button
              className="text-button"
              onClick={() => {
                void bootstrap.refetch();
                void conversations.refetch();
              }}
            >
              Retry
            </button>
          </div>
        )}
        <div className="sidebar-footer">
          <div className="workspace-note">
            <ShieldCheck size={16} />
            <div>
              <strong>Built for confident decisions</strong>
              <span>Verified data. Confirmed actions.</span>
            </div>
          </div>
          <button className="settings-button" onClick={() => setSettingsOpen(true)}>
            <SettingsIcon size={17} />
            <span>Workspace settings</span>
            <span className="version">v1.0</span>
          </button>
          <div className="profile">
            <span className="profile-avatar">F</span>
            <div>
              <strong>Finance workspace</strong>
              <span>Local development profile</span>
            </div>
            <ChevronDown size={14} />
          </div>
        </div>
      </aside>
      <div className="main-workspace">
        <header className="topbar">
          <div className="topbar-title">
            <button
              className="mobile-menu icon-button"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open navigation"
            >
              <Menu size={20} />
            </button>
            <span className="header-icon">
              <Sparkles size={17} />
            </span>
            <div>
              <h2>{current.data?.title || 'Finance workspace'}</h2>
              <span>
                {conversationId ? 'Conversation' : 'Overview'}
                <span className="header-breadcrumb"> / Finance Assistant</span>
              </span>
            </div>
          </div>
          <div className="header-controls">
            <ConnectionBadge status={effectiveStatus} onClick={() => setSettingsOpen(true)} />
            <span className="company-pill">
              {(effectiveStatus?.company || capabilities.data?.company || 'USMF').toUpperCase()}
            </span>
            <button
              className="header-settings icon-button"
              onClick={() => setSettingsOpen(true)}
              aria-label="Open settings"
            >
              <SettingsIcon size={18} />
            </button>
          </div>
        </header>
        {capabilities.data?.mock_mode && (
          <div className="mock-ribbon">
            <span>DEMO MODE</span>Mock Dynamics 365 records · No live ERP connection or financial
            posting
          </div>
        )}
        <ConnectionBanner
          status={effectiveStatus}
          onReconnect={() => reconnect.mutate()}
          isReconnecting={reconnect.isPending}
        />
        <main className="chat-workspace">
          <div className="chat-scroll">
            {current.isError && (
              <div className="workspace-error" role="alert">
                <strong>Conversation could not be loaded</strong>
                <p>{current.error.message}</p>
                <button className="button secondary small" onClick={() => void current.refetch()}>
                  Retry
                </button>
              </div>
            )}
            {messages.isError && (
              <div className="workspace-error" role="alert">
                <strong>Conversation history is unavailable</strong>
                <p>{messages.error.message}</p>
                <button className="button secondary small" onClick={() => void messages.refetch()}>
                  Retry loading history
                </button>
              </div>
            )}
            {conversationId && messages.isLoading ? (
              <div className="message-skeleton" aria-label="Loading messages">
                <div />
                <div />
                <div />
              </div>
            ) : selectedMessages.length === 0 && !messages.isError ? (
              <Welcome
                onPrompt={(text) => {
                  setComposer(text);
                }}
                capabilities={capabilities.data}
                onSettings={() => setSettingsOpen(true)}
              />
            ) : (
              <div className="messages">
                {selectedMessages.map((message) => (
                  <MessageView
                    key={message.id}
                    message={message}
                    conversationId={conversationId || ''}
                    onEvidence={openEvidence}
                    onRetry={retry}
                    onSpeak={speech.speak}
                    canSpeak={speech.supported && preferences.speechEnabled}
                    streaming={!!activeGeneration && message.id === generation?.messageId}
                    onUpdated={() => void refresh()}
                    onError={notifyError}
                  />
                ))}
                {activeGeneration && generation?.tool && (
                  <div className="tool-activity" aria-live="polite">
                    <Spinner label={generation.tool} />
                  </div>
                )}
                <div ref={bottom} />
              </div>
            )}
          </div>
          <div className="composer-area">
            <div className="workspace-capabilities">
              <TrustNote />
              <div>
                <span>
                  <Cpu size={13} />
                  {capabilities.data?.model || 'gpt-4.1-mini'}
                </span>
                <span
                  title={
                    capabilities.data?.voice_input
                      ? 'Voice transcription available'
                      : 'Voice transcription is not configured'
                  }
                >
                  <Mic size={13} />
                  {capabilities.data?.voice_input ? 'Voice ready' : 'Text mode'}
                </span>
              </div>
            </div>
            {speech.speaking && (
              <div className="speech-controls" role="status">
                <VolumeLabel />
                <span>{speech.paused ? 'Speech paused' : 'Reading response'}</span>
                <button
                  className="icon-button"
                  onClick={speech.paused ? speech.resume : speech.pause}
                  aria-label={speech.paused ? 'Resume speech' : 'Pause speech'}
                >
                  {speech.paused ? <Play size={14} /> : <Pause size={14} />}
                </button>
                <button className="icon-button" onClick={speech.stop} aria-label="Stop speech">
                  <Square size={12} />
                </button>
              </div>
            )}
            {generation && !activeGeneration && (
              <div className="generation-elsewhere">
                A response is generating in another conversation.
                <button
                  className="text-button"
                  onClick={() => navigate(`/chat/${generation.conversationId}`)}
                >
                  View response
                </button>
              </div>
            )}
            <Composer
              value={composer}
              onChange={setComposer}
              onSend={(text) => void send(text)}
              onStop={stopGeneration}
              streaming={!!generation}
              disabled={
                creating ||
                bootstrap.isLoading ||
                bootstrap.isError ||
                (conversationId ? messages.isLoading || messages.isError || current.isError : false)
              }
              capabilities={capabilities.data}
              autoSendTranscript={preferences.autoSendTranscript}
              onError={notifyError}
            />
            {creating && (
              <div className="creating-status">
                <Spinner label="Creating your conversation…" />
              </div>
            )}
          </div>
        </main>
      </div>
      <EvidencePanel records={evidence} open={evidenceOpen} onOpenChange={setEvidenceOpen} />
      <Settings
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        preferences={preferences}
        updatePreferences={updatePreferences}
        status={effectiveStatus}
        capabilities={capabilities.data}
        onReconnect={() => reconnect.mutate()}
        reconnecting={reconnect.isPending}
        voices={speech.voices}
        speechSupported={speech.supported}
        conversationId={conversationId}
        onError={notifyError}
        onSuccess={(message) => {
          notifySuccess(message);
          void client.invalidateQueries({ queryKey: ['conversations'] });
        }}
        sessionReady={bootstrap.isSuccess}
      />
      <Modal
        open={!!conversationAction}
        onOpenChange={(open) => {
          if (!open && !actionBusy) setConversationAction(null);
        }}
        title={
          conversationAction?.kind === 'rename' ? 'Rename conversation' : 'Delete conversation?'
        }
        description={
          conversationAction?.kind === 'rename'
            ? 'Choose a clear title for this finance conversation.'
            : 'This conversation will no longer appear in your history. Local records are retained for auditing.'
        }
        className="small-dialog"
      >
        {conversationAction?.kind === 'rename' ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void completeConversationAction();
            }}
          >
            <label className="field-label" htmlFor="conversation-title">
              Conversation title
            </label>
            <input
              id="conversation-title"
              value={newTitle}
              onChange={(event) => setNewTitle(event.target.value)}
              maxLength={120}
              autoFocus
            />
            <div className="dialog-actions">
              <button
                type="button"
                className="button secondary"
                onClick={() => setConversationAction(null)}
              >
                Cancel
              </button>
              <button type="submit" className="button" disabled={actionBusy || !newTitle.trim()}>
                {actionBusy ? 'Saving…' : 'Save title'}
              </button>
            </div>
          </form>
        ) : (
          <div className="dialog-actions">
            <button
              className="button secondary"
              onClick={() => setConversationAction(null)}
              disabled={actionBusy}
            >
              Keep conversation
            </button>
            <button
              className="button danger"
              onClick={() => void completeConversationAction()}
              disabled={actionBusy}
            >
              {actionBusy ? 'Deleting…' : 'Delete conversation'}
            </button>
          </div>
        )}
      </Modal>
      {toast && <Toast {...toast} onClose={() => setToast(null)} />}
    </div>
  );
}
function VolumeLabel() {
  return (
    <span className="speech-pulse">
      <Sparkles size={14} />
    </span>
  );
}
