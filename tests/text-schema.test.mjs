import test from "node:test";
import assert from "node:assert/strict";
import { isoDatetimeOffset, utf16 } from "../lib/text-schema.ts";
import { createJobSchema, idSchema, metadataSchema } from "../lib/contracts.ts";

test("ids keep zod 3's acceptance: any 8-4-4-4-12 hex, no version or variant check", () => {
  for (const ok of ["00000000-0000-0000-0000-000000000000", "ABCDEF01-2345-0789-0BCD-EF0123456789", "6b7e1c1e-0a51-4c7c-9b55-0d1f1f7d0e11"]) assert.equal(idSchema.safeParse(ok).success, true, ok);
  for (const bad of [" 6b7e1c1e-0a51-4c7c-9b55-0d1f1f7d0e11", "6b7e1c1e-0a51-4c7c-9b55-0d1f1f7d0e1", "6b7e1c1e0a514c7c9b550d1f1f7d0e11", "../x"]) assert.equal(idSchema.safeParse(bad).success, false, bad);
});

test("date-times keep zod 3's acceptance: optional seconds and +HHMM offsets, still no local times", () => {
  for (const ok of ["2026-11-27T10:00:00+09:00", "2026-11-27T10:00:00.123Z", "2026-11-27T10:00+09:00", "2026-11-27T10:00:00+0900", "2028-02-29T00:00Z"]) {
    assert.equal(isoDatetimeOffset().safeParse(ok).success, true, ok);
    assert.equal(metadataSchema.safeParse({ datetime: ok }).success, true, ok);
  }
  for (const bad of ["2026-11-27T10:00:00", "2026-11-27 10:00:00+09:00", "2026-02-29T10:00Z", "2026-11-27T24:00Z", "2026-11-27T10:00:00+09", "x"]) assert.equal(isoDatetimeOffset().safeParse(bad).success, false, bad);
  assert.equal(metadataSchema.safeParse({ datetime: "" }).success, true, "empty stays allowed");
});

// zod 4 counts code points; the app's limits stay in UTF-16 code units (one emoji = 2), as with zod 3.
test("utf16 limits count UTF-16 code units, not code points", () => {
  assert.equal(utf16(4).safeParse("😀😀").success, true);
  assert.equal(utf16(3).safeParse("😀😀").success, false);
  assert.equal(utf16(10, 2).safeParse("a").success, false);
  assert.equal(utf16(10, 1).safeParse("").success, false);
  assert.equal(utf16(10).optional().safeParse(undefined).success, true);
});

test("contract fields reject an emoji string that only fits when counted in code points", () => {
  const org = "😀".repeat(100); // 100 code points, 200 UTF-16 units; organization allows 150
  assert.equal(metadataSchema.safeParse({ organization: org }).success, false);
  assert.equal(metadataSchema.safeParse({ organization: "😀".repeat(75) }).success, true);
  const job = { upload_id: "6b7e1c1e-0a51-4c7c-9b55-0d1f1f7d0e11", filename: "😀".repeat(128) + ".m4a", byte_size: 10, mime_type: "audio/mp4", output_mode: "transcript", input_kind: "file" };
  assert.equal(createJobSchema.safeParse(job).success, false, "a 260-unit file name is over 255");
  assert.equal(createJobSchema.safeParse({ ...job, filename: "회의.m4a" }).success, true);
});
