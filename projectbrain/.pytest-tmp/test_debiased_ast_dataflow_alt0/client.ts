const API_BASE = 'https://api.example.com';
export async function triggerDocValidation(docId: string) {
    const res = await fetch(`${API_BASE}/api/v1/docs/${docId}/validate`, { method: 'POST' });
    const payload = await res.json();
    return payload.missing_client_field;
}
