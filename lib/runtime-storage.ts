import { DatabaseSync } from "node:sqlite";
import { createHash, randomUUID } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import { chmod, lstat, mkdir, open, readFile, readdir, rename, rm } from "node:fs/promises";
import { isAbsolute, join } from "node:path";
import { Readable } from "node:stream";
import type { Bucket, Database, ObjectInfo, Statement } from "./runtime-types.ts";

export class SQLiteDatabase implements Database {
  readonly connection: DatabaseSync;
  constructor(path: string) {
    this.connection = new DatabaseSync(path);
    this.connection.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;");
  }
  prepare(sql: string): Statement {
    const statement = this.connection.prepare(sql);
    const make = (values: unknown[]): Statement => ({
      bind: (...args) => make(args),
      async first<T>() { return (statement.get(...values as never[]) as T | undefined) ?? null; },
      async all<T>() { return { results: statement.all(...values as never[]) as T[] }; },
      async run() { const result=statement.run(...values as never[]);return { meta: { changes: Number(result.changes) } }; },
    });
    return make([]);
  }
  close() { this.connection.close(); }
}
export async function privateDirectory(path: string) {
  if (!isAbsolute(path)) throw new Error("MEETING_DATA_DIR must be absolute");
  await mkdir(path, { recursive: true, mode: 0o700 });
  const info = await lstat(path); if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("Unsafe data directory");
  await chmod(path, 0o700);
}
const keyHash = (key: string) => createHash("sha256").update(key).digest("hex");
type Stored = ObjectInfo & { size: number; version: 1 };
async function syncDirectory(path:string){if(process.platform==="win32")return;const dir=await open(path,"r");try{await dir.sync();}finally{await dir.close();}}
export class FileBucket implements Bucket {
  readonly root: string;
  constructor(root: string) { if (!isAbsolute(root)) throw new Error("Bucket path must be absolute");this.root=root; }
  private path(key: string) { if (!key || key.length > 1024 || /[\x00-\x1f]/.test(key)) throw new Error("Invalid object key");return join(this.root, keyHash(key)); }
  private async info(key: string): Promise<Stored | null> {
    const dir = this.path(key);
    try {
      const info=await lstat(dir); if(!info.isDirectory() || info.isSymbolicLink()) throw new Error("Unsafe object directory");
      const meta=JSON.parse(await readFile(join(dir,"meta.json"),"utf8")) as Stored;
      if(meta.key!==key || meta.version!==1 || !Number.isSafeInteger(meta.size) || meta.size<0)throw new Error("Invalid object metadata");
      return meta;
    } catch(e) { if((e as NodeJS.ErrnoException).code==="ENOENT")return null;throw e; }
  }
  async head(key: string) { return this.info(key); }
  async put(key: string, body: Uint8Array, options: { onlyIf: { etagDoesNotMatch: string }; customMetadata: Record<string,string> }) {
    if (options.onlyIf.etagDoesNotMatch !== "*") throw new Error("Only immutable publication supported");
    await privateDirectory(this.root);
    const target=this.path(key), staging=join(this.root,`.tmp-${keyHash(key)}-${randomUUID()}`);
    await mkdir(staging,{mode:0o700});
    const metadata:Stored={version:1,key,size:body.byteLength,customMetadata:options.customMetadata};
    try {
      // Metadata first also lets deletion recover an interrupted, unpublished write.
      for(const [name,content] of [["meta.json",JSON.stringify(metadata)],["body",body]] as const){const f=await open(join(staging,name),"wx",0o600);try{await f.writeFile(content);await f.sync();}finally{await f.close();}}
      await syncDirectory(staging);
      // Atomic nonempty-directory rename publishes data + metadata together. A competing
      // writer cannot replace an existing nonempty object directory on Linux or Windows.
      try { await rename(staging,target); }
      catch(e){if(await this.info(key))return null;throw e;}
      await syncDirectory(this.root);
      return metadata;
    } finally { await rm(staging,{recursive:true,force:true}); }
  }
  async get(key: string, options?: {range:{offset:number;length:number}}) {
    const meta=await this.info(key);if(!meta)return null;
    const start=options?.range.offset??0,length=options?.range.length??meta.size;
    if(!Number.isSafeInteger(start)||!Number.isSafeInteger(length)||start<0||length<0||start+length>meta.size)throw new Error("Invalid object range");
    const file=await open(join(this.path(key),"body"),fsConstants.O_RDONLY|fsConstants.O_NOFOLLOW);
    const stream=length?Readable.toWeb(file.createReadStream({start,end:start+length-1,autoClose:true}),{strategy:{highWaterMark:64*1024,size:(chunk:Uint8Array)=>chunk.byteLength}}):new ReadableStream<Uint8Array>({start(c){c.close();void file.close();}});
    return {...meta,body:stream as ReadableStream<Uint8Array>};
  }
  async delete(keys:string|string[]) {
    let entries:string[];try{entries=await readdir(this.root);}catch(e){if((e as NodeJS.ErrnoException).code==="ENOENT")return;throw e;}
    for(const key of Array.isArray(keys)?keys:[keys]){
      const target=this.path(key),hash=keyHash(key),tomb=join(this.root,`.delete-${hash}-${randomUUID()}`);
      try{await rename(target,tomb);}catch(e){if((e as NodeJS.ErrnoException).code!=="ENOENT")throw e;}
      await rm(tomb,{recursive:true,force:true});
      for(const entry of entries)if(entry.startsWith(`.delete-${hash}-`)||entry.startsWith(`.tmp-${hash}-`))await rm(join(this.root,entry),{recursive:true,force:true});
    }
    await syncDirectory(this.root);
  }
  async list({prefix,cursor}:{prefix:string;cursor?:string}) {
    let entries:string[];try{entries=await readdir(this.root);}catch(e){if((e as NodeJS.ErrnoException).code==="ENOENT")entries=[];else throw e;}
    const unique=new Map<string,Stored>();
    for(const entry of entries){const hash=entry.match(/^(?:\.(?:tmp|delete)-)?([a-f0-9]{64})(?:-[a-f0-9-]+)?$/)?.[1];if(!hash)continue;try{const meta=JSON.parse(await readFile(join(this.root,entry,"meta.json"),"utf8")) as Stored;if(meta.key.startsWith(prefix)&&(!cursor||meta.key>cursor)&&keyHash(meta.key)===hash)unique.set(meta.key,meta);}catch(e){if((e as NodeJS.ErrnoException).code!=="ENOENT")throw e;}}
    const all=[...unique.values()];
    all.sort((a,b)=>a.key<b.key?-1:a.key>b.key?1:0);const objects=all.slice(0,1000),truncated=all.length>1000;
    return {objects,truncated,...(truncated?{cursor:objects.at(-1)!.key}:{})};
  }
}
