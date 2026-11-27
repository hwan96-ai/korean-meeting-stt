CREATE TABLE `jobs` (
	`id` text PRIMARY KEY NOT NULL,
	`owner` text NOT NULL,
	`state` text NOT NULL,
	`revision` integer DEFAULT 1 NOT NULL,
	`generation` integer DEFAULT 1 NOT NULL,
	`data` text NOT NULL,
	`created_at` integer NOT NULL,
	`updated_at` integer NOT NULL,
	`lease_id` text,
	`lease_until` integer,
	`deleted_at` integer
);
--> statement-breakpoint
CREATE TABLE `revisions` (
	`key` text PRIMARY KEY NOT NULL,
	`job_id` text NOT NULL,
	`kind` text NOT NULL,
	`revision` integer NOT NULL,
	`data` text NOT NULL,
	`created_at` integer NOT NULL
);
--> statement-breakpoint
CREATE TABLE `settings` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `upload_parts` (
	`key` text PRIMARY KEY NOT NULL,
	`job_id` text NOT NULL,
	`part` integer NOT NULL,
	`bytes` integer NOT NULL,
	`sha256` text NOT NULL
);
