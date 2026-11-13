// Raster tile cache for server-rendered layout tiles.
// A tile (L, ix, iy) covers [ix*W, (ix+1)*W] x [iy*W, (iy+1)*W] dbu, W = TILE * 2^L.

export interface TileReq {
  key: string; // full key (project, version, mask, depth, level, ix, iy)
  pos: string; // level|ix|iy (version independent)
  url: string;
  prio: number;
}

interface Entry {
  bmp: ImageBitmap | null;
  used: number;
  retryAt?: number; // the request failed: asked again from then on
}

const MAX_TILES = 700;
const MAX_INFLIGHT = 8;
// A failed tile (the server restarting or busy, a network error) is asked for again after a pause that doubles with
// each failure; it stayed blank until the view moved before.
const RETRY_MS = 1000;
const RETRY_MAX_MS = 30000;

export class TileCache {
  private tiles = new Map<string, Entry>();
  // last loaded bitmap for a tile position, any version: shown while the new one loads
  private stale = new Map<string, ImageBitmap>();
  private queue: TileReq[] = [];
  private inflight = new Map<string, AbortController>();
  private failures = new Map<string, number>();
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private clock = 0;

  constructor(private onLoad: () => void) {}

  get(key: string): ImageBitmap | null {
    const e = this.tiles.get(key);
    if (!e) return null;
    e.used = ++this.clock;
    return e.bmp;
  }

  getStale(pos: string): ImageBitmap | null {
    return this.stale.get(pos) ?? null;
  }

  // Replaces the wanted set; requests are served nearest-first.
  want(reqs: TileReq[]): void {
    const wanted = new Set(reqs.map((r) => r.key));
    for (const [key, ac] of this.inflight) {
      if (!wanted.has(key)) {
        ac.abort();
        this.inflight.delete(key);
        this.tiles.delete(key);
      }
    }
    const now = Date.now();
    for (const r of reqs) {
      const e = this.tiles.get(r.key);
      if (e?.retryAt !== undefined && e.retryAt <= now) this.tiles.delete(r.key);
    }
    this.queue = reqs.filter((r) => !this.tiles.has(r.key)).sort((a, b) => a.prio - b.prio);
    this.pump();
  }

  private pump(): void {
    while (this.inflight.size < MAX_INFLIGHT && this.queue.length) {
      const r = this.queue.shift()!;
      if (this.tiles.has(r.key)) continue;
      const ac = new AbortController();
      const entry: Entry = { bmp: null, used: ++this.clock };
      this.tiles.set(r.key, entry);
      this.inflight.set(r.key, ac);
      fetch(r.url, { signal: ac.signal })
        .then((res) => {
          if (!res.ok) throw new Error(String(res.status));
          return res.blob();
        })
        .then((blob) => createImageBitmap(blob))
        .then((bmp) => {
          entry.bmp = bmp;
          this.failures.delete(r.key);
          const old = this.stale.get(r.pos);
          this.stale.set(r.pos, bmp);
          if (old && old !== bmp && !this.isLive(old)) old.close();
          this.onLoad();
        })
        .catch(() => {
          if (ac.signal.aborted) {
            this.tiles.delete(r.key);
            return;
          }
          const n = (this.failures.get(r.key) ?? 0) + 1;
          const wait = Math.min(RETRY_MAX_MS, RETRY_MS * 2 ** (n - 1));
          this.failures.set(r.key, n);
          entry.retryAt = Date.now() + wait;
          const timer = setTimeout(() => { this.timers.delete(timer); this.onLoad(); }, wait);   // redraw: want() asks again
          this.timers.add(timer);
        })
        .finally(() => {
          this.inflight.delete(r.key);
          this.evict();
          this.pump();
        });
    }
  }

  private isLive(bmp: ImageBitmap): boolean {
    for (const e of this.tiles.values()) if (e.bmp === bmp) return true;
    return false;
  }

  private evict(): void {
    if (this.tiles.size <= MAX_TILES) return;
    const entries = [...this.tiles.entries()].filter(([k]) => !this.inflight.has(k));
    entries.sort((a, b) => a[1].used - b[1].used);
    const staleSet = new Set(this.stale.values());
    for (const [k, e] of entries.slice(0, this.tiles.size - MAX_TILES)) {
      this.tiles.delete(k);
      if (e.bmp && !staleSet.has(e.bmp)) e.bmp.close();
    }
    if (this.stale.size > MAX_TILES * 2) {
      const live = new Set([...this.tiles.values()].map((e) => e.bmp));
      for (const [pos, bmp] of this.stale) {
        if (this.stale.size <= MAX_TILES) break;
        if (!live.has(bmp)) {
          bmp.close();
          this.stale.delete(pos);
        }
      }
    }
  }

  clear(): void {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
    this.failures.clear();
    for (const ac of this.inflight.values()) ac.abort();
    this.inflight.clear();
    this.queue = [];
    this.tiles.clear();
    this.stale.clear();
  }
}
