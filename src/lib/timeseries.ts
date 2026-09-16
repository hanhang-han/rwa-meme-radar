import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";

export interface Point {
  t: number;
  v: number;
  p?: string;
}

export class Timeseries {
  private series = new Map<string, Point[]>();

  constructor(private file: string, private maxPoints: number, private bucketMs: number) {}

  load() {
    try {
      if (!existsSync(this.file)) return;
      const raw = JSON.parse(readFileSync(this.file, "utf-8")) as Record<string, Point[]>;
      for (const [k, v] of Object.entries(raw)) this.series.set(k, v);
      console.log(`[ts] loaded ${this.series.size} series from ${this.file}`);
    } catch (e) {
      console.error("[ts] load failed:", e instanceof Error ? e.message : e);
    }
  }

  push(key: string, v: number, p?: string) {
    let arr = this.series.get(key);
    if (!arr) {
      arr = [];
      this.series.set(key, arr);
    }
    const t = Math.floor(Date.now() / this.bucketMs) * this.bucketMs;
    if (arr.length && arr[arr.length - 1].t === t) {
      arr[arr.length - 1].v = v;
      if (p) arr[arr.length - 1].p = p;
    } else {
      arr.push({ t, v, ...(p ? { p } : {}) });
    }
    if (arr.length > this.maxPoints) arr.splice(0, arr.length - this.maxPoints);
  }

  get(key: string, maxPoints = this.maxPoints): Point[] {
    const arr = this.series.get(key) ?? [];
    return arr.slice(-maxPoints);
  }

  pointAt(key: string, atMs: number): Point | undefined {
    const arr = this.series.get(key) ?? [];
    let best: Point | undefined;
    for (const p of arr) {
      if (p.t <= atMs) best = p;
      else break;
    }
    return best;
  }

  keys(): string[] {
    return [...this.series.keys()];
  }

  save() {
    try {
      mkdirSync("data", { recursive: true });
      writeFileSync(this.file, JSON.stringify(Object.fromEntries(this.series)));
    } catch (e) {
      console.error("[ts] save failed:", e instanceof Error ? e.message : e);
    }
  }
}

export const ts = new Timeseries("data/ts.json", 10080, 60_000);
export const tsHourly = new Timeseries("data/ts_hourly.json", 192, 3_600_000);
