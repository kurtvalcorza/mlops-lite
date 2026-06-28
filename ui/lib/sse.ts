// Minimal SSE reader over fetch() (we POST to open a stream, so EventSource — GET-only — won't do).
// Parses `data:` frames from the BFF-proxied gateway stream and yields each decoded JSON event.

export type SseEvent = Record<string, unknown> & { event?: string };

export async function* streamSse(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent> {
  const r = await fetch(`/api/gw/${path.replace(/^\//, '')}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
    body: JSON.stringify(body),
    cache: 'no-store',
    signal,
  });
  if (!r.ok || !r.body) {
    const detail = await r.text().catch(() => '');
    throw new Error(`stream ${path} -> ${r.status}: ${detail.slice(0, 200)}`);
  }

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line.
    let sep: number;
    while ((sep = buf.indexOf('\n\n')) !== -1) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of frame.split('\n')) {
        const s = line.trim();
        if (!s.startsWith('data:')) continue;
        const payload = s.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        try {
          yield JSON.parse(payload) as SseEvent;
        } catch {
          /* ignore keep-alive / non-JSON frames */
        }
      }
    }
  }
}
