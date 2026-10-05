import { ArrowUpRight, Building2, FileText, ShieldCheck } from 'lucide-react';
import type { EvidenceRecord } from '../types';
import { formatDate, label, money, displayValue } from '../lib/format';
import { Modal } from './ui';
export function EvidenceCards({
  records,
  onOpen,
}: {
  records: EvidenceRecord[];
  onOpen: () => void;
}) {
  const invoices = records.filter((record) => record.invoice_number);
  const visible = invoices.length ? invoices.slice(0, 4) : records.slice(0, 2);
  return (
    <div className="evidence-preview">
      <div className="evidence-preview-label">
        <ShieldCheck size={14} />
        Finance evidence<span>{records.length} records</span>
      </div>
      {visible.map((record, index) => (
        <button
          key={`${record.invoice_number || record.account || record.kind}-${index}`}
          className="invoice-card"
          onClick={onOpen}
        >
          <span className="invoice-icon">
            {record.invoice_number ? <FileText size={17} /> : <Building2 size={17} />}
          </span>
          <span className="invoice-info">
            <strong>
              {record.invoice_number ||
                record.customer_name ||
                record.account ||
                label(record.kind || 'Finance record')}
            </strong>
            <span>
              {record.due_date
                ? `Due ${formatDate(record.due_date)}`
                : record.source_entity || 'Dynamics 365'}{' '}
              · {record.company?.toUpperCase() || 'ERP'}
            </span>
          </span>
          <span className="invoice-amount">
            {record.remaining_amount !== undefined ? (
              <>
                <strong>{money(record.remaining_amount, record.currency)}</strong>
                <span>Remaining</span>
              </>
            ) : (
              <span>{record.currency || 'View record'}</span>
            )}
          </span>
          <ArrowUpRight size={14} />
        </button>
      ))}
      <button className="text-button evidence-view" onClick={onOpen}>
        Inspect source records <ArrowUpRight size={13} />
      </button>
    </div>
  );
}
export function EvidencePanel({
  records,
  open,
  onOpenChange,
}: {
  records: EvidenceRecord[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const fields: (keyof EvidenceRecord)[] = [
    'customer_name',
    'account',
    'company',
    'currency',
    'invoice_number',
    'external_invoice_id',
    'original_amount',
    'remaining_amount',
    'due_date',
    'voucher',
    'source_entity',
    'retrieved_at',
  ];
  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="ERP evidence"
      description="Source records retrieved for this response. Amounts and references come from the finance service."
      className="evidence-drawer"
    >
      <div className="evidence-drawer-intro">
        <ShieldCheck size={19} />
        <span>
          {records.length} source {records.length === 1 ? 'record' : 'records'} · Read-only evidence
        </span>
      </div>
      <div className="evidence-scroll">
        {records.length === 0 ? (
          <p className="muted">No ERP source records are attached to this response.</p>
        ) : (
          records.map((record, index) => (
            <article className="evidence-record" key={index}>
              <h3>
                <FileText size={17} />
                {record.invoice_number ||
                  record.customer_name ||
                  record.account ||
                  `Finance record ${index + 1}`}
              </h3>
              <dl>
                {fields
                  .filter(
                    (field) =>
                      record[field] !== undefined && record[field] !== null && record[field] !== '',
                  )
                  .map((field) => (
                    <div key={field}>
                      <dt>{label(String(field))}</dt>
                      <dd>
                        {field === 'original_amount' || field === 'remaining_amount'
                          ? money(record[field] as string | number, record.currency)
                          : field === 'retrieved_at'
                            ? formatDate(record.retrieved_at, true)
                            : field === 'due_date'
                              ? formatDate(record.due_date)
                              : displayValue(record[field])}
                      </dd>
                    </div>
                  ))}
              </dl>
              <details>
                <summary>Full source record</summary>
                <pre>{JSON.stringify(record, null, 2)}</pre>
              </details>
            </article>
          ))
        )}
      </div>
    </Modal>
  );
}
