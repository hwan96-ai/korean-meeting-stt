import { z } from "zod";

// String length in UTF-16 code units, as zod 3 measured it and as the web, SQLite rows and the worker count it.
// zod 4's .min()/.max() count Unicode code points instead, so an emoji-heavy value could pass twice the limit.
export function utf16(max: number, min = 0) {
  const schema = min > 0 ? z.string().refine(v => v.length >= min, { message: `${min}자 이상이어야 합니다.` }) : z.string();
  return schema.refine(v => v.length <= max, { message: `${max}자(UTF-16) 이하여야 합니다.` });
}

// ISO date-time with a Z or numeric offset, exactly as zod 3's z.string().datetime({ offset: true }) accepted it:
// seconds and fractions optional, offset +HH:MM or +HHMM. zod 4 requires seconds and the colon, which would turn
// an older client's retry (same external_ref) into a 422.
const ISO_OFFSET = /^((\d\d[2468][048]|\d\d[13579][26]|\d\d0[48]|[02468][048]00|[13579][26]00)-02-29|\d{4}-((0[13578]|1[02])-(0[1-9]|[12]\d|3[01])|(0[469]|11)-(0[1-9]|[12]\d|30)|(02)-(0[1-9]|1\d|2[0-8])))T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d(\.\d+)?)?(Z|([+-]\d{2}:?\d{2}))$/;
export function isoDatetimeOffset() {
  return z.string().regex(ISO_OFFSET, { message: "날짜와 시각은 시간대가 붙은 ISO 형식이어야 합니다." });
}
