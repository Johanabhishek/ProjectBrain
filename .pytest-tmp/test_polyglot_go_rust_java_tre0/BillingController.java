package com.example.billing;

import java.util.List;

@RequestMapping("/api/v1/billing")
public class BillingController {
    @PostMapping("/charge")
    public String chargeCustomer(String customerId) {
        return auditCharge(customerId);
    }

    public String auditCharge(String customerId) {
        return customerId;
    }
}
