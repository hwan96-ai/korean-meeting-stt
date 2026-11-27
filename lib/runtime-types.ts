export interface Statement {
  bind(...values: unknown[]): Statement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  run(): Promise<{ meta: { changes: number } }>;
}
export interface Database { prepare(sql: string): Statement; }
export interface ObjectInfo { key: string; customMetadata?: Record<string, string>; }
export interface Bucket {
  put(key: string, body: Uint8Array, options: { onlyIf: { etagDoesNotMatch: string }; customMetadata: Record<string, string> }): Promise<ObjectInfo | null>;
  head(key: string): Promise<ObjectInfo | null>;
  get(key: string, options?: { range: { offset: number; length: number } }): Promise<(ObjectInfo & { body: ReadableStream<Uint8Array> }) | null>;
  delete(keys: string | string[]): Promise<void>;
  list(options: { prefix: string; cursor?: string }): Promise<{ objects: ObjectInfo[]; truncated: boolean; cursor?: string }>;
}
export interface Identity { userId: string; email: string; displayName: string; fullName: string | null; }
