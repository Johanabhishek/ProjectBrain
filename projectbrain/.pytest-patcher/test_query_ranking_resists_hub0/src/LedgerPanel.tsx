export function LedgerPanel({ ledger }) {
  const total = ledger.reduce((a, b) => a + b.amount, 0);
  return total;
}
