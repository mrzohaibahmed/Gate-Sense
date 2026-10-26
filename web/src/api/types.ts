/** Named shortcuts for the API's types (all generated from the OpenAPI schema). */
import type { components } from "./schema";

type S = components["schemas"];

export type Me = S["CurrentUser"];
export type Site = S["Site"];
export type Person = S["Person"];
export type PersonDetail = S["PersonDetail"];
export type Camera = S["Camera"];
export type WorkerInfo = S["WorkerInfo"];
export type AttendanceRow = S["AttendanceRow"];
export type AttendanceEvent = S["Event"];
export type Overview = S["Overview"];
export type CameraStatus = S["CameraStatus"];
export type AuditEntry = S["AuditEntry"];
export type Setting = S["Setting"];
export type UserOut = S["UserOut"];
export type Status = AttendanceRow["status"];
export type Direction = AttendanceEvent["direction"];
export type HourCount = S["HourCount"];
export type StepInfo = S["StepInfo"];
export type FrameCheck = S["FrameCheck"];
export type EnrollmentSource = S["EnrollmentSource"];
export type Review = S["Review"];
export type SystemInfo = S["SystemInfo"];
export type Backup = S["Backup"];
export type Roi = S["Roi"];
export type TestResult = S["TestResult"];
export type DailyRow = S["DailyRow"];
export type PersonTotal = S["PersonTotal"];
export type PhotoResult = S["PhotoResult"];
export type CameraLog = S["CameraLog"];
export type ImportPreview = S["ImportPreview"];
export type ImportRowOut = S["ImportRowOut"];
