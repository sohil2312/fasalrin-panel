# Application entry modes implementation plan

Goal: run Old/Fresh from one branch list with accurate preflight counts.
Architecture: shared eligibility logic, existing entry scripts and existing progress storage. No extra work-list copies.
Tech stack: Python, CSV, vanilla JavaScript, unittest, Playwright.

- [ ] Add shared filter/preview module with regression tests for validation, progress and once-only retry handling.
- [ ] Integrate selection before browser launch in both scripts; preserve the regular progress header for shared fresh runs.
- [ ] Add fresh job mapping and preview API data; recheck eligibility on Start.
- [ ] Add two entry controls, filter summary, strict empty-queue disabling and updated column guidance.
- [ ] Test backend, browser, progress compatibility and zero-ready behavior; update docs; commit and push.
