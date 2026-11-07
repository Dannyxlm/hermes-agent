/** Synthetic wire edge; the real HermesGateway and JSON-RPC channel run above it. */
export class FamilyWireSocket extends EventTarget {
  static OPEN = 1
  static CLOSED = 3
  static calls: Array<{ url: string; method: string; params: Record<string, unknown> }> = []
  static answer: (method: string, params: Record<string, unknown>) => object = () => ({ result: {} })
  readyState = 0
  constructor(readonly url: string) {
    super()
    queueMicrotask(() => { this.readyState = 1; this.dispatchEvent(new Event('open')) })
  }
  send(payload: string) {
    const frame = JSON.parse(payload)

    if (!frame.method) { return }
    FamilyWireSocket.calls.push({ url: this.url, method: frame.method, params: frame.params || {} })
    const reply = FamilyWireSocket.answer(frame.method, frame.params || {})
    queueMicrotask(() => this.dispatchEvent(new MessageEvent('message', {
      data: JSON.stringify({ jsonrpc: '2.0', id: frame.id, ...reply })
    })))
  }
  close() { this.readyState = 3; this.dispatchEvent(new Event('close')) }
}
