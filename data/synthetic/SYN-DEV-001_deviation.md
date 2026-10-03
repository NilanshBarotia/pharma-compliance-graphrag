---
doc_id: SYN-DEV-001
doc_type: deviation_report
synthetic: true
disclaimer: FICTIONAL deviation report for a software exercise.
---
# Deviation Report DEV-2026-031

**Title:** Compression force sensor on TP-03 used beyond calibration due date
**Opened:** 2026-05-19  **Category:** Equipment  **Severity:** Major  **Status:** Closed (investigation complete; CAPA in progress)
**Site:** Riverbend Solid Dose Facility  **Batch affected:** Lot MX26-014 (Meridax 20 mg Tablets)

## Description
During QA review of the executed record for MX26-014, the reviewer noted that the compression-force sensor on Tablet Press 3 (TP-03) was last calibrated 2025-11-04 and was due again 2026-05-04. The batch was compressed 14 days after the due date.

## Investigation
- Calibration is governed by SOP-MNT-014, *Calibration of Production Equipment*. The scheduling tool did not flag the press because the asset was recorded as "Compression Line 3 press" in the maintenance system, so the due-date reminder was never generated.
- Root cause: asset naming mismatch between the batch record system and the maintenance scheduling system.
- The procedure requiring deviations to be recorded and justified was followed (SOP-QA-021, *Deviation Management*).

## Regulatory relevance
Routine calibration of automatic/electronic equipment per a written program (21 CFR 211.68(a)); written procedures followed and deviations recorded (21 CFR 211.100(b)). SOP-MNT-014 implements 21 CFR 211.68(a); SOP-QA-021 implements 21 CFR 211.100(b).

## Linked records
CAPA-2026-011.
