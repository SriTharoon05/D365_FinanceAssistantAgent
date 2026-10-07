import { useState, useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BarChart3, LineChart, RotateCcw, Table2 } from 'lucide-react';
import { chartImageUrl, endpoints } from '../lib/api';
import type { ChartDescriptor } from '../types';
import { Spinner } from './ui';

function subscribeToTheme(onChange: () => void) {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  return () => observer.disconnect();
}

function darkTheme() {
  return document.documentElement.classList.contains('dark');
}

function subscribeToCompactScreen(onChange: () => void) {
  const media = window.matchMedia('(max-width: 640px)');
  media.addEventListener('change', onChange);
  return () => media.removeEventListener('change', onChange);
}

function compactScreen() {
  return window.matchMedia('(max-width: 640px)').matches;
}

function ChartImage({
  messageId,
  chart,
  theme,
  compact,
}: {
  messageId: string;
  chart: ChartDescriptor;
  theme: 'light' | 'dark';
  compact: boolean;
}) {
  const [status, setStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const [attempt, setAttempt] = useState(0);
  const source = compact
    ? chartImageUrl(messageId, chart.id, theme, 'compact')
    : chartImageUrl(messageId, chart.id, theme);
  const compactBar = compact && chart.kind === 'bar';
  const imageHeight = compactBar
    ? Math.max(280, chart.points.length * 24 + 90)
    : compact
      ? 280
      : 300;
  return (
    <div className={`chart-visual${compactBar ? ' chart-visual-compact-bar' : ''}`}>
      {status === 'error' ? (
        <div className="chart-error" role="status">
          <p>The chart image is unavailable. You can still review the source amounts in Data.</p>
          <button
            className="button secondary small"
            onClick={() => {
              setStatus('loading');
              setAttempt((previous) => previous + 1);
            }}
          >
            <RotateCcw size={15} />
            Retry chart
          </button>
        </div>
      ) : (
        <>
          {status === 'loading' && (
            <div className="chart-loading">
              <Spinner label="Loading chart…" />
            </div>
          )}
          <img
            className={`chart-image ${status === 'loading' ? 'is-loading' : ''}`}
            src={attempt ? `${source}&reload=${attempt}` : source}
            alt={`${chart.title}, ${chart.company}, ${chart.currency}`}
            width={compact ? 360 : 720}
            height={imageHeight}
            loading="lazy"
            decoding="async"
            onLoad={() => setStatus('loaded')}
            onError={() => setStatus('error')}
          />
        </>
      )}
    </div>
  );
}

export function FinanceCharts({ messageId }: { messageId: string }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<'chart' | 'data'>('chart');
  const theme = useSyncExternalStore(subscribeToTheme, darkTheme, () => false) ? 'dark' : 'light';
  const compact = useSyncExternalStore(subscribeToCompactScreen, compactScreen, () => false);
  const { data } = useQuery({
    queryKey: ['message-charts', messageId],
    queryFn: () => endpoints.charts(messageId),
    enabled: Boolean(messageId) && !messageId.startsWith('local-'),
    staleTime: Infinity,
    retry: 1,
    refetchOnWindowFocus: false,
  });
  const charts = data?.charts || [];
  const chart = charts.find((item) => item.id === selectedId) || charts[0];
  if (!chart) return null;
  const Icon = chart.kind === 'line' ? LineChart : BarChart3;
  return (
    <section className="finance-charts" aria-label="Finance charts">
      <div className="chart-heading">
        <Icon size={18} aria-hidden="true" />
        <h3>{chart.title}</h3>
        <span className="chart-currency">
          {chart.company.toUpperCase()} · {chart.currency}
        </span>
      </div>
      <p className="chart-description">{chart.description}</p>
      <div className="chart-controls">
        {charts.length > 1 && (
          <label className="chart-selector">
            <span>View</span>
            <select
              aria-label="Finance chart"
              value={chart.id}
              onChange={(event) => setSelectedId(event.target.value)}
            >
              {charts.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.title} · {item.company} · {item.currency}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="chart-view-controls" role="group" aria-label="Chart presentation">
          <button
            className={view === 'chart' ? 'active' : ''}
            aria-pressed={view === 'chart'}
            onClick={() => setView('chart')}
          >
            <Icon size={15} aria-hidden="true" />
            Chart
          </button>
          <button
            className={view === 'data' ? 'active' : ''}
            aria-pressed={view === 'data'}
            onClick={() => setView('data')}
          >
            <Table2 size={15} aria-hidden="true" />
            Data
          </button>
        </div>
      </div>
      {view === 'chart' ? (
        <>
          <ChartImage
            key={`${messageId}:${chart.id}:${theme}:${compact}`}
            messageId={messageId}
            chart={chart}
            theme={theme}
            compact={compact}
          />
          {chart.kind === 'bar' && (
            <div className="chart-legend" aria-label="Chart categories">
              {chart.points.slice(0, 3).map((point, index) => (
                <span key={`${point.label}:${index}`}>{point.label}</span>
              ))}
              <button className="text-button" onClick={() => setView('data')}>
                {chart.points.length > 3 ? 'View all categories' : 'View exact amounts'}
              </button>
            </div>
          )}
        </>
      ) : (
        <div className="chart-data table-container">
          <table aria-label={`${chart.title} data`}>
            <thead>
              <tr>
                <th scope="col">{chart.kind === 'line' ? 'Period' : 'Category'}</th>
                <th scope="col">Amount ({chart.currency})</th>
              </tr>
            </thead>
            <tbody>
              {chart.points.map((point, index) => (
                <tr key={`${point.label}:${index}`}>
                  <th scope="row">{point.label}</th>
                  <td className="chart-amount">
                    {chart.currency} {point.amount}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="chart-source-note">
        Based on {chart.source_count} source record{chart.source_count === 1 ? '' : 's'} in this
        response.
      </p>
    </section>
  );
}
