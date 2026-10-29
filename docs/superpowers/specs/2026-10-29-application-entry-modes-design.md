# Old and fresh application entry

Approved scope: two entry choices in Loan Applications, using the same uploaded master and selected branch work list. Old runs the existing regular script; Fresh runs the existing fresh script. Approval and claims stay unchanged.

Old requires positive DP and valid Aadhaar. Fresh additionally requires DOB, relative name, village, survey number, khata number, and positive land area, for all schemes as requested. Existing account, date, identity, scheme, preview and duplicate safeguards remain. Fresh also needs the applicant name, gender and address consumed by its existing workflow. The summary identifies validation failures instead of silently proceeding with incomplete data.

Before Start, show total rows, ready, filtered unique rows, already finished/held rows, and per-reason counts. One row can contribute to multiple reason counts. Filtered rows are never deleted. Completed and uncertain-submission records are protected across both modes. The optional Aadhaar-only filter affects Old eligibility.

Use the same CSV and regular progress schema for both modes; standalone fresh lists keep their existing schema. Recompute eligibility before launching and within each script before opening a browser. Reject empty queues with a useful message.

Tests cover both modes, missing/zero fields, overlapping reasons, completed/held status, progress schema compatibility, shared records and backend job routing. Browser checks cover mode switching, counts, empty queues and unchanged approval controls. No live submissions during verification.
