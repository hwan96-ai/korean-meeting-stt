CREATE TABLE `integration_calls` (
	`external_ref` text PRIMARY KEY NOT NULL,
	`job_id` text NOT NULL,
	`created_at` integer NOT NULL,
	`direction` text NOT NULL,
	`started_at` text NOT NULL,
	`duration_sec` integer NOT NULL,
	`counterpart_name` text,
	`phone_e164` text,
	`crm_contact_id` text,
	`crm_company_id` text,
	`file_size` integer NOT NULL,
	`file_sha256` text NOT NULL,
	`file_mime` text NOT NULL,
	`file_ext` text NOT NULL,
	`reverting_at` integer,
	`reverted_at` integer,
	`job_deleted_at` integer
);
--> statement-breakpoint
CREATE UNIQUE INDEX `integration_calls_job_id_unique` ON `integration_calls` (`job_id`);--> statement-breakpoint
CREATE UNIQUE INDEX `integration_calls_live_file_unique` ON `integration_calls` (`file_sha256`) WHERE reverted_at IS NULL AND job_deleted_at IS NULL;--> statement-breakpoint
CREATE INDEX `integration_calls_created_at` ON `integration_calls` (`created_at`);--> statement-breakpoint
-- Hand-written (drizzle-kit does not generate triggers). Deleting a job, by any code version, clears the
-- person's name and number of its call and marks the row as no longer holding its file, in the same statement.
-- If integration_calls ever has to be dropped, DROP this trigger first (otherwise every job deletion fails).
CREATE TRIGGER `integration_calls_clear_on_job_delete` AFTER UPDATE OF `deleted_at` ON `jobs` WHEN NEW.`deleted_at` IS NOT NULL BEGIN UPDATE `integration_calls` SET `phone_e164`=NULL, `counterpart_name`=NULL, `job_deleted_at`=COALESCE(`job_deleted_at`, NEW.`deleted_at`) WHERE `job_id`=NEW.`id`; END;
