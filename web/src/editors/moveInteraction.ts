import type { Schematic } from "../api";

export type MoveRequest = { items: { k: "i" | "w"; id: string }[]; dx: number; dy: number };
export type MoveResult = { result: string[]; view: Schematic };
export type MoveStatus = "idle" | "pending" | "ready" | "blocked" | "committing";
export type MoveState = { result: MoveResult | null; status: MoveStatus };

/** Coalesce pointer updates without overlapping server work or flashing the original drawing. */
export class MovePreview {
  private version = 0;
  private key = "";
  private busy = false;
  private disposed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: { input: MoveRequest; version: number; key: string } | null = null;
  private result: MoveResult | null = null;

  constructor(
    private readonly preview: (input: MoveRequest) => Promise<MoveResult>,
    private readonly show: (state: MoveState) => void,
    private readonly delay = 30,
  ) {}

  request(input: MoveRequest): void {
    const key = JSON.stringify(input);
    if (this.disposed || key === this.key) return;
    this.key = key;
    this.pending = { input: { ...input, items: input.items.map((item) => ({ ...item })) }, version: this.version, key };
    this.show({ result: this.result, status: "pending" });
    // Keep the existing deadline so continuous pointer events cannot postpone
    // all feedback until the user stops moving.
    if (this.timer === null) this.timer = setTimeout(() => { this.timer = null; void this.run(); }, this.delay);
  }

  /** Keep the last valid drawing while the one atomic edit is being saved. */
  hold(): void {
    this.stopPending();
    if (!this.disposed) this.show({ result: this.result, status: "committing" });
  }

  clear(): void {
    this.stopPending();
    this.result = null;
    if (!this.disposed) this.show({ result: null, status: "idle" });
  }

  dispose(): void {
    this.stopPending();
    this.result = null;
    this.disposed = true;
  }

  private stopPending(): void {
    this.version++;
    this.key = "";
    this.pending = null;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private async run(): Promise<void> {
    if (this.busy || this.disposed || !this.pending) return;
    const pending = this.pending;
    this.pending = null;
    this.busy = true;
    try {
      const result = await this.preview(pending.input);
      if (!this.disposed && pending.version === this.version) {
        this.result = result;
        // One request is in flight, so completed positions are always ordered.
        // Show progress even if the pointer moved again during the request;
        // the current target remains outlined until its geometry arrives.
        this.show({ result, status: pending.key === this.key ? "ready" : "pending" });
      }
    } catch {
      if (!this.disposed && pending.version === this.version) {
        this.show({ result: this.result, status: pending.key === this.key ? "blocked" : "pending" });
      }
    } finally {
      this.busy = false;
      if (this.timer === null) void this.run();
    }
  }
}
