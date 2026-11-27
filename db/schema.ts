import { sql } from "drizzle-orm";
import { sqliteTable, text, integer, index, uniqueIndex } from "drizzle-orm/sqlite-core";
export const settings = sqliteTable("settings", { key: text("key").primaryKey(), value: text("value").notNull() });
export const jobs = sqliteTable("jobs", {
  id: text("id").primaryKey(), owner: text("owner").notNull(), state: text("state").notNull(),
  revision: integer("revision").notNull().default(1), generation: integer("generation").notNull().default(1),
  data: text("data").notNull(), createdAt: integer("created_at").notNull(), updatedAt: integer("updated_at").notNull(),
  leaseId: text("lease_id"), leaseUntil: integer("lease_until"), deletedAt: integer("deleted_at"),
});
export const uploadParts = sqliteTable("upload_parts", {
  key: text("key").primaryKey(), jobId: text("job_id").notNull(), part: integer("part").notNull(),
  bytes: integer("bytes").notNull(), sha256: text("sha256").notNull(),
});
export const revisions = sqliteTable("revisions", {
  key: text("key").primaryKey(), jobId: text("job_id").notNull(), kind: text("kind").notNull(),
  revision: integer("revision").notNull(), data: text("data").notNull(), createdAt: integer("created_at").notNull(),
});
// Calls handed over by the orchestrator (/api/integration/v1/calls). One row per external_ref; the full
// phone number lives only here and is cleared when the job is deleted or reverted.
export const integrationCalls = sqliteTable("integration_calls", {
  externalRef: text("external_ref").primaryKey(), jobId: text("job_id").notNull().unique(), createdAt: integer("created_at").notNull(),
  direction: text("direction").notNull(), startedAt: text("started_at").notNull(), durationSec: integer("duration_sec").notNull(),
  counterpartName: text("counterpart_name"), phoneE164: text("phone_e164"), crmContactId: text("crm_contact_id"), crmCompanyId: text("crm_company_id"),
  fileSize: integer("file_size").notNull(), fileSha256: text("file_sha256").notNull(), fileMime: text("file_mime").notNull(), fileExt: text("file_ext").notNull(),
  // jobDeletedAt: copied by the trigger when the job is deleted (owner or revert); a deleted job no longer holds its file.
  revertingAt: integer("reverting_at"), revertedAt: integer("reverted_at"), jobDeletedAt: integer("job_deleted_at"),
}, table => [
  // One live call per recording: the same file under another ref is refused (lib/integration.ts duplicate_file).
  uniqueIndex("integration_calls_live_file_unique").on(table.fileSha256).where(sql`reverted_at IS NULL AND job_deleted_at IS NULL`),
  // Daily intake limit counts by creation time.
  index("integration_calls_created_at").on(table.createdAt),
]);
