/** Synthetic wire edge; the real HermesGateway and JSON-RPC channel run above it. */
export class FamilyWireSocket extends EventTarget {
  static OPEN = 1
  static CLOSED = 3
  static calls: Array<{ url: string; method: string; params: Record<string, unknown> }> = []
  static answer: (method: string, params: Record<string, unknown>) => object = () => ({ result: {} })
  readyState = 0
  constructor(readonly url: string) {
    super()
    queueMicrotask(() => {
      this.readyState = 1
      this.dispatchEvent(new Event('open'))
      // A public dial counts as connected only once its own socket says which session wire it
      // speaks (`gateway.ready`); a native dial is canonical by provenance and ignores the frame.
      this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({
        jsonrpc: '2.0', method: 'event',
        params: { type: 'gateway.ready', payload: { session_authority: new URL(this.url).searchParams.has('native_dial') } }
      }) }))
    })
  }
  send(payload: string) {
    const frame = JSON.parse(payload)

    if (!frame.method) { return }

    // The shared client announces `client.capabilities` itself once a public dial is ready; `calls`
    // records only what the code under test asked for.
    if (frame.method !== 'client.capabilities') {
      FamilyWireSocket.calls.push({ url: this.url, method: frame.method, params: frame.params || {} })
    }

    const reply = FamilyWireSocket.answer(frame.method, frame.params || {})
    queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ jsonrpc: '2.0', id: frame.id, ...reply })
    })))
  }
  close() { this.readyState = 3; this.dispatchEvent(new Event('close')) }
}
