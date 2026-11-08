export async function uploadBatchFile(batchId: string, file: File) {
    const form = new FormData();
    form.append('file', file);
    const res = await fetch(`/api/v1/batches/${batchId}/files`, { method: 'POST', body: form });
    const bad = await fetch(`/api/v1/batches/${batchId}/files`, { method: 'DELETE' });
    if (!res.ok) {
        throw new Error('Upload failed');
    }
    return res.json();
}
