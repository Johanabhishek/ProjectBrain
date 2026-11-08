package main

import "net/http"

type OrderHandler struct {}

func (h *OrderHandler) CreateOrder(w http.ResponseWriter, r *http.Request) {
    ValidateOrderPayload(r)
}

func ValidateOrderPayload(r *http.Request) bool {
    return true
}

func RegisterRoutes(router *GinEngine) {
    router.POST("/api/orders", CreateOrder)
}
