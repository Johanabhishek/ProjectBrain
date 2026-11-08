<template><div>{{ count }}</div></template>
<script lang="ts">
export function computeWidgetTotal(price: number, qty: number): number {
  return price * qty;
}
</script>
