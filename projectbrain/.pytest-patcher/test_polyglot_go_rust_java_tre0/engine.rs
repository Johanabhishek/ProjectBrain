use std::collections::HashMap;

pub struct PaymentEngine {}

impl PaymentEngine {
    pub fn settle_invoice(&self, invoice_id: u64) -> bool {
        verify_ledger_entry(invoice_id)
    }
}

#[get("/api/invoices")]
pub fn list_invoices() -> String {
    String::from("ok")
}

pub fn verify_ledger_entry(id: u64) -> bool {
    id > 0
}
