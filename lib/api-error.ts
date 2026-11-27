// Plain class fields (no parameter properties) so Node's type stripping can load this file in tests.
export class ApiError extends Error {
  status: number; code: string;
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code; }
}
export const fail = (status: number, code: string, message: string): never => { throw new ApiError(status, code, message); };
