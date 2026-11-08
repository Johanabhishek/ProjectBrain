using System;
public class OrderController {
    [HttpPost("/api/orders")]
    public string CreateOrder(string orderId) {
        return orderId;
    }
}
