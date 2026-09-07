---
name: rt-signal-validation
description: Evaluate scanner and response anomalies with controls, reproducibility, and explicit limitations.
budget: 500
---
Use the current authorized scope and tool permissions. Record the feature, identity context, hypothesis, and expected behavior before testing. Load only the procedure relevant to that hypothesis.

Preserve baseline, test, and control response receipts and compare them with compare_evidence. Retain original responses. Check authentication state, redirects, cache behavior, rate limits, transient server failures, and response truncation before drawing conclusions.

Reflected input is not demonstrated script execution. An error signature is not demonstrated data access. A single slow request is not a repeatable timing effect. A version match is not proof that a vulnerable configuration is present. Keep these as candidates until the claimed behavior is observed.

Write the strongest alternative explanation and identify an observation that could disprove your hypothesis. Collect that control and a separate reproduction within the existing engagement limits. If the environment cannot support a reliable control, report inconclusive rather than clean or confirmed.

Before reporting, call review_finding with exact receipt-bound quotes and explicit impact limitations. Coverage describes the checks performed, not the security of the entire application. Do not escalate a candidate merely because its suggested impact is severe.
