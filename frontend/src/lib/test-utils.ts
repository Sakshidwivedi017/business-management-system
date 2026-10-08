import type { SessionStore } from "./session.ts";

/** In-memory stand-in for window.sessionStorage. */
export class MemoryStore implements SessionStore {
  private items = new Map<string, string>();

  get length(): number {
    return this.items.size;
  }

  key(index: number): string | null {
    return [...this.items.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.items.set(key, String(value));
  }

  removeItem(key: string): void {
    this.items.delete(key);
  }

  keys(): string[] {
    return [...this.items.keys()];
  }
}
