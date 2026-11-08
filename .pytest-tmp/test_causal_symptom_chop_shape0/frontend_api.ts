export async function uploadBatchFile(batchId: string, file: File) {
    const res = await fetch(`/api/v1/batches/${batchId}/files`, { method: 'POST' });
    const data = await res.json();
    return data.non_existent_prop;
}
