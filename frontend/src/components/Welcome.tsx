import {
  ArrowUpRight,
  Building2,
  CalendarClock,
  Mail,
  Receipt,
  PlusCircle,
  ChartNoAxesCombined,
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
  capabilities,
  onSettings,
}: {
  capabilities?: Capabilities;
  onSettings: () => void;
}) {
  return (
    <div className="welcome">
      <div className="welcome-orb" aria-hidden="true">
        <span className="welcome-orb-ring" />
        <span className="welcome-orb-core" />
      </div>
      <div className="welcome-intro">
        <p className="welcome-kicker">Your finance, made clear.</p>
        <h1>
          How can I help you <span>today?</span>
        </h1>
        <p className="welcome-description">
          Explore customers, review invoices, and prepare your next action with Dynamics 365
          Finance.
        </p>
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
    </div>
  );
}

export function WelcomeSuggestions({
  onPrompt,
  showTestAction = true,
}: {
  onPrompt: (value: string) => void;
  showTestAction?: boolean;
}) {
  return (
    <div className="welcome-examples">
      <div className="suggestion-grid" aria-label="Suggested finance questions">
        {suggestions.map(({ title, text, subtitle, Icon }) => (
          <button
            key={title}
            className="suggestion-card"
            title={subtitle}
            onClick={() => onPrompt(text)}
          >
            <span className="suggestion-icon">
              <Icon size={16} />
            </span>
            <strong>{title}</strong>
          </button>
        ))}
      </div>
      <div className="chart-shortcuts" aria-label="Suggested finance charts">
        <span>
          <ChartNoAxesCombined size={14} /> Quick charts
        </span>
        <button onClick={() => onPrompt("Show Asterion's outstanding invoices as a chart.")}>
          Invoices
        </button>
        <button onClick={() => onPrompt("Show Asterion's overdue invoices as a chart.")}>
          Overdue
        </button>
        <button onClick={() => onPrompt("Show Asterion's payment history as a chart.")}>
          Payments
        </button>
      </div>
      {showTestAction && (
        <div className="welcome-workflow">
          <button
            className="text-button"
            onClick={() => onPrompt('Create a test customer called TEST-ACME-001.')}
          >
            <PlusCircle size={15} />
            Create a test customer <ArrowUpRight size={13} />
          </button>
        </div>
      )}
    </div>
  );
}
