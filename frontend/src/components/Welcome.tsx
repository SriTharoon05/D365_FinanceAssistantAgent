import {
  ArrowUpRight,
  Building2,
  CalendarClock,
  FileSpreadsheet,
  Mail,
  Receipt,
  ShieldCheck,
  Sparkles,
  PlusCircle,
} from 'lucide-react';
import type { Capabilities } from '../types';
const suggestions = [
  {
    title: 'Customer balance',
    text: "What is Asterion's outstanding balance?",
    subtitle: 'See balances with supporting invoices',
    Icon: Building2,
  },
  {
    title: 'Overdue invoices',
    text: 'Which Asterion invoices are overdue?',
    subtitle: 'Prioritize your collections',
    Icon: CalendarClock,
  },
  {
    title: 'Payment history',
    text: "Show Asterion's payment history.",
    subtitle: 'Trace payments and references',
    Icon: Receipt,
  },
  {
    title: 'Collection reminder',
    text: 'Draft a collection reminder for Asterion.',
    subtitle: 'Prepare a draft grounded in ERP data',
    Icon: Mail,
  },
];
export function Welcome({
  onPrompt,
  capabilities,
  onSettings,
}: {
  onPrompt: (value: string) => void;
  capabilities?: Capabilities;
  onSettings: () => void;
}) {
  return (
    <div className="welcome">
      <div className="welcome-kicker">
        <span className="welcome-symbol">
          <FileSpreadsheet size={24} />
        </span>
        <span>Your finance workspace</span>
      </div>
      <h1>
        What would you like
        <br />
        <span>to review today?</span>
      </h1>
      <p className="welcome-description">
        Explore balances, understand invoices, and prepare your next action with evidence from
        Dynamics 365 Finance.
      </p>
      <div className="welcome-trust">
        <span>
          <ShieldCheck size={14} />
          ERP-backed answers
        </span>
        <span>
          <Sparkles size={14} />
          Safe, confirmed actions
        </span>
      </div>
      {capabilities && !capabilities.ai_configured && !capabilities.mock_mode && (
        <div className="welcome-config">
          <strong>Connect your AI assistant</strong>
          <p>
            Add the Azure OpenAI API key in the backend environment to enable chat. Configure
            Dynamics 365 credentials there for live finance data.
          </p>
          <button className="text-button" onClick={onSettings}>
            View connected capabilities <ArrowUpRight size={13} />
          </button>
        </div>
      )}
      <div className="suggestion-grid">
        {suggestions.map(({ title, text, subtitle, Icon }) => (
          <button key={title} className="suggestion-card" onClick={() => onPrompt(text)}>
            <span className="suggestion-icon">
              <Icon size={19} />
            </span>
            <span>
              <strong>{title}</strong>
              <small>{subtitle}</small>
            </span>
            <ArrowUpRight size={15} />
          </button>
        ))}
      </div>
      <div className="welcome-workflow">
        <div>
          <PlusCircle size={16} />
          <strong>Prepare a finance action</strong>
        </div>
        <p>
          Create a test customer or a draft invoice. Review the proposed changes before anything is
          written.
        </p>
        <button
          className="text-button"
          onClick={() => onPrompt('Create a test customer called TEST-ACME-001.')}
        >
          Create a test customer <ArrowUpRight size={13} />
        </button>
      </div>
    </div>
  );
}
