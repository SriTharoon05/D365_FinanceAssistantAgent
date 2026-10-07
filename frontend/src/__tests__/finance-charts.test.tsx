import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FinanceCharts } from '../components/FinanceCharts';
import { endpoints } from '../lib/api';
import type { ChartDescriptor } from '../types';

const remaining: ChartDescriptor = {
  id: 'remaining-usmf-inr',
  kind: 'bar',
  title: 'Open amounts by due date',
  company: 'USMF',
  currency: 'INR',
  description: 'Current remaining amounts grouped by due date.',
  points: [
    { label: 'Overdue', amount: '35000.00' },
    { label: 'Not yet due', amount: '75000.00' },
  ],
  source_count: 2,
};
const payments: ChartDescriptor = {
  id: 'payments-usrt-usd',
  kind: 'line',
  title: 'Payments by month',
  company: 'USRT',
  currency: 'USD',
  description: 'Payment amounts grouped by payment month.',
  points: [
    { label: '2026-09', amount: '9007199254740993.01' },
    { label: '2026-10', amount: '-0.25' },
  ],
  source_count: 3,
};

function renderCharts(messageId = 'message-1') {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0, gcTime: 0 } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <FinanceCharts messageId={messageId} />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

afterEach(() => document.documentElement.classList.remove('dark'));

describe('finance chart presentation', () => {
  it('keeps exact source amounts accessible when the chart image fails and supports an image retry', async () => {
    const discovery = vi.spyOn(endpoints, 'charts').mockResolvedValue({ charts: [remaining] });
    renderCharts('message/1');
    const image = await screen.findByAltText('Open amounts by due date, USMF, INR');
    expect(image).toHaveAttribute(
      'src',
      '/api/messages/message%2F1/charts/remaining-usmf-inr.png?theme=light',
    );
    expect(image).toHaveAttribute('loading', 'lazy');
    fireEvent.error(image);

    expect(screen.getByRole('status')).toHaveTextContent('You can still review the source amounts');
    await userEvent.click(screen.getByRole('button', { name: 'Retry chart' }));
    const retriedImage = screen.getByAltText('Open amounts by due date, USMF, INR');
    expect(retriedImage).toHaveAttribute(
      'src',
      '/api/messages/message%2F1/charts/remaining-usmf-inr.png?theme=light&reload=1',
    );
    fireEvent.error(retriedImage);
    await userEvent.click(screen.getByRole('button', { name: 'Data' }));

    const table = screen.getByRole('table', { name: 'Open amounts by due date data' });
    expect(within(table).getByRole('row', { name: 'Overdue INR 35000.00' })).toBeVisible();
    expect(within(table).getByRole('row', { name: 'Not yet due INR 75000.00' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Data' })).toHaveAttribute('aria-pressed', 'true');
    expect(discovery).toHaveBeenCalledExactlyOnceWith('message/1');
  });

  it('switches company and currency groups without mixing amounts or losing decimal precision', async () => {
    const discovery = vi
      .spyOn(endpoints, 'charts')
      .mockResolvedValue({ charts: [remaining, payments] });
    renderCharts();
    await screen.findByRole('combobox', { name: 'Finance chart' });
    await userEvent.click(screen.getByRole('button', { name: 'Data' }));
    expect(screen.getByText('INR 35000.00')).toBeVisible();

    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Finance chart' }),
      payments.id,
    );

    const table = screen.getByRole('table', { name: 'Payments by month data' });
    expect(within(table).getByRole('columnheader', { name: 'Period' })).toBeVisible();
    expect(screen.getByText('USRT · USD')).toBeVisible();
    expect(within(table).getByText('USD 9007199254740993.01')).toBeVisible();
    expect(within(table).getByText('USD -0.25')).toBeVisible();
    expect(screen.queryByText('INR 35000.00')).not.toBeInTheDocument();
    expect(discovery).toHaveBeenCalledTimes(1);
  });

  it('reloads the proxy image for the current theme without querying finance data again', async () => {
    const discovery = vi.spyOn(endpoints, 'charts').mockResolvedValue({ charts: [remaining] });
    renderCharts();
    const light = await screen.findByAltText('Open amounts by due date, USMF, INR');
    fireEvent.load(light);
    expect(screen.queryByText('Loading chart…')).not.toBeInTheDocument();

    await act(async () => document.documentElement.classList.add('dark'));

    const dark = screen.getByAltText('Open amounts by due date, USMF, INR');
    expect(dark).toHaveAttribute(
      'src',
      '/api/messages/message-1/charts/remaining-usmf-inr.png?theme=dark',
    );
    expect(screen.getByText('Loading chart…')).toBeVisible();
    expect(discovery).toHaveBeenCalledTimes(1);
  });

  it('requests a readable compact image on narrow screens and resets loading when the screen widens', async () => {
    let compact = true;
    const listeners = new Set<() => void>();
    const media = {
      get matches() {
        return compact;
      },
      addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener),
    } as unknown as MediaQueryList;
    vi.spyOn(window, 'matchMedia').mockReturnValue(media);
    const discovery = vi.spyOn(endpoints, 'charts').mockResolvedValue({ charts: [remaining] });
    const { unmount } = renderCharts();

    const small = await screen.findByAltText('Open amounts by due date, USMF, INR');
    expect(small).toHaveAttribute(
      'src',
      '/api/messages/message-1/charts/remaining-usmf-inr.png?theme=light&size=compact',
    );
    expect(small).toHaveAttribute('width', '360');
    expect(small).toHaveAttribute('height', '280');
    fireEvent.load(small);
    expect(screen.queryByText('Loading chart…')).not.toBeInTheDocument();

    await act(async () => {
      compact = false;
      listeners.forEach((notify) => notify());
    });

    const large = screen.getByAltText('Open amounts by due date, USMF, INR');
    expect(large).toHaveAttribute(
      'src',
      '/api/messages/message-1/charts/remaining-usmf-inr.png?theme=light',
    );
    expect(large).toHaveAttribute('width', '720');
    expect(large).toHaveAttribute('height', '300');
    expect(screen.getByText('Loading chart…')).toBeVisible();
    expect(discovery).toHaveBeenCalledTimes(1);
    unmount();
    expect(listeners.size).toBe(0);
  });

  it('keeps messages with no chart data free of empty chart panels', async () => {
    vi.spyOn(endpoints, 'charts').mockResolvedValue({ charts: [] });
    const { client } = renderCharts();
    await waitFor(() =>
      expect(client.getQueryState(['message-charts', 'message-1'])?.status).toBe('success'),
    );
    expect(screen.queryByRole('region', { name: 'Finance charts' })).not.toBeInTheDocument();
  });

  it('retries discovery at most once and hides an unavailable discovery service', async () => {
    const discovery = vi.spyOn(endpoints, 'charts').mockRejectedValue(new Error('Unavailable'));
    const { client } = renderCharts();
    await waitFor(() =>
      expect(client.getQueryState(['message-charts', 'message-1'])?.status).toBe('error'),
    );
    expect(discovery).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('region', { name: 'Finance charts' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('does not request charts for an unsaved local message', () => {
    const discovery = vi.spyOn(endpoints, 'charts');
    renderCharts('local-unsaved');
    expect(discovery).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Finance charts' })).not.toBeInTheDocument();
  });
});
