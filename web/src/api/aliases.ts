// Stable names for generated enum types. json-schema-to-typescript numbers same-titled
// literals (`Status`, `Status1`, …) in schema order, so a new field elsewhere can swap them;
// code names these aliases instead.
import type { Finding, Review } from "./types.gen";

export type ClauseStatus = Finding["status"];
export type ReviewStatus = Review["status"];
export type FindingStatus = Finding["finding_status"];
