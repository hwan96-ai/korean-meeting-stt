import { DatabaseSync } from "node:sqlite";
import { readdir, readFile, chmod, lstat } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { privateDirectory } from "../lib/runtime-storage.ts";

const directory=process.env.MEETING_DATA_DIR,owner=process.env.MEETING_OWNER_ID;
if(!directory||!owner)throw new Error("MEETING_DATA_DIR and MEETING_OWNER_ID required");
await privateDirectory(directory);await privateDirectory(join(directory,"objects"));
const path=join(directory,"meeting.sqlite");
try{if((await lstat(path)).isSymbolicLink())throw new Error("Unsafe SQLite file");}catch(e){if(e.code!=="ENOENT")throw e;}
const db=new DatabaseSync(path);
try{
  db.exec("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;");
  db.exec("CREATE TABLE IF NOT EXISTS runtime_migrations(name TEXT PRIMARY KEY NOT NULL)");
  const folder=fileURLToPath(new URL("../drizzle/",import.meta.url));
  for(const name of (await readdir(folder)).filter(n=>/^\d+.*\.sql$/.test(n)).sort()){
    if(db.prepare("SELECT name FROM runtime_migrations WHERE name=?").get(name))continue;
    db.exec("BEGIN IMMEDIATE");
    try{db.exec(await readFile(join(folder,name),"utf8"));db.prepare("INSERT INTO runtime_migrations(name) VALUES(?)").run(name);db.exec("COMMIT");}catch(e){db.exec("ROLLBACK");throw e;}
  }
  db.exec("BEGIN IMMEDIATE");
  try{
    const saved=db.prepare("SELECT value FROM settings WHERE key='owner'").get();
    const foreign=db.prepare("SELECT COUNT(*) AS count FROM jobs WHERE owner<>?").get(owner);
    if((saved&&saved.value!==owner)||foreign.count)throw new Error("Owner mismatch: explicit offline migration required; refusing automatic reassignment");
    db.prepare("INSERT OR IGNORE INTO settings(key,value) VALUES('owner',?)").run(owner);
    db.exec("COMMIT");
  }catch(e){db.exec("ROLLBACK");throw e;}
  db.exec("PRAGMA wal_checkpoint(TRUNCATE)");await chmod(path,0o600);
  console.log("OCI schema and fixed owner ready (no credentials printed)");
}finally{db.close();}
