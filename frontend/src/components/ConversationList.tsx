import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Archive, MessageSquare, MoreHorizontal, PenLine, Plus, Trash2 } from 'lucide-react';
import type { Conversation } from '../types';
interface Props {
  conversations: Conversation[];
  activeId?: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (conversation: Conversation) => void;
  onArchive: (conversation: Conversation) => void;
  onDelete: (conversation: Conversation) => void;
  isLoading?: boolean;
}
export function ConversationList({
  conversations,
  activeId,
  onSelect,
  onNew,
  onRename,
  onArchive,
  onDelete,
  isLoading,
}: Props) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const week = new Date(today);
  week.setDate(today.getDate() - 7);
  const groups = [
    { title: 'Today', match: (c: Conversation) => new Date(c.updated_at) >= today },
    {
      title: 'Previous 7 days',
      match: (c: Conversation) => new Date(c.updated_at) < today && new Date(c.updated_at) >= week,
    },
    { title: 'Older', match: (c: Conversation) => new Date(c.updated_at) < week },
  ];
  return (
    <>
      <button className="new-chat button" onClick={onNew}>
        <Plus size={17} />
        New conversation<span className="shortcut">⌘ K</span>
      </button>
      <div className="conversation-list" aria-label="Conversation history">
        {isLoading ? (
          <div className="history-skeleton" aria-label="Loading conversations">
            <div />
            <div />
            <div />
          </div>
        ) : conversations.length === 0 ? (
          <div className="history-empty">
            <MessageSquare size={23} />
            <p>No conversations yet</p>
            <span>Your finance workspace starts here.</span>
          </div>
        ) : (
          groups.map((group) => {
            const items = conversations.filter(group.match);
            return (
              items.length > 0 && (
                <section key={group.title}>
                  <h3>{group.title}</h3>
                  {items.map((conversation) => (
                    <div
                      key={conversation.id}
                      className={`conversation-item ${activeId === conversation.id ? 'active' : ''}`}
                    >
                      <button
                        className="conversation-select"
                        onClick={() => onSelect(conversation.id)}
                        aria-current={activeId === conversation.id ? 'page' : undefined}
                      >
                        <MessageSquare size={15} />
                        <span>{conversation.title}</span>
                      </button>
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger
                          className="conversation-menu icon-button"
                          aria-label={`Options for ${conversation.title}`}
                        >
                          <MoreHorizontal size={16} />
                        </DropdownMenu.Trigger>
                        <DropdownMenu.Portal>
                          <DropdownMenu.Content
                            className="dropdown-content"
                            align="start"
                            sideOffset={5}
                          >
                            <DropdownMenu.Item onSelect={() => onRename(conversation)}>
                              <PenLine size={14} />
                              Rename
                            </DropdownMenu.Item>
                            <DropdownMenu.Item onSelect={() => onArchive(conversation)}>
                              <Archive size={14} />
                              {conversation.archived_at ? 'Restore' : 'Archive'}
                            </DropdownMenu.Item>
                            <DropdownMenu.Separator />
                            <DropdownMenu.Item
                              className="danger"
                              onSelect={() => onDelete(conversation)}
                            >
                              <Trash2 size={14} />
                              Delete conversation
                            </DropdownMenu.Item>
                          </DropdownMenu.Content>
                        </DropdownMenu.Portal>
                      </DropdownMenu.Root>
                    </div>
                  ))}
                </section>
              )
            );
          })
        )}
      </div>
    </>
  );
}
