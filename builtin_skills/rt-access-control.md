---
name: rt-access-control
description: Investigate permission boundaries with controlled identities and evidence-backed disproof checks.
budget: 550
---
Use only the current engagement's authorized targets and test identities. Do not infer authorization from a discovered credential.

Before testing, identify the feature, intended account roles, and expected permission boundary. If the intended policy is unknown, keep the result inconclusive. Plan each feature and identity in security_coverage.

Collect a normal response with the intended account, the relevant test response, and a separate control using existing scope-aware tools. Record exact URLs, session labels, and evidence receipt IDs. Account labels describe supplied context; they do not prove which account the server authenticated. Check session validity before interpreting the result.

Use compare_evidence to inspect status, redirects, response differences, and identity fingerprints. A login page, cached public data, different UI text, or a status change does not by itself demonstrate unauthorized access. Demonstrate only the minimum behavior needed to test the stated boundary.

Disproof checks: is the resource intentionally public, is this the same authorized account, does the response omit protected content, or did authentication expire? Capture a control observation that addresses the most plausible alternative explanation. Repeat the relevant observation independently.

Call review_finding with exact observation quotes, reproduction and control receipt IDs, expected boundary, demonstrated impact, alternative explanation, disproof check, and limitations. Keep unsupported conclusions as candidates. Do not generalize one endpoint's result to other accounts or features.

Include challenge.test: the recorded HTTP or browser method, tested input (none and an empty name for an endpoint-only check), and a specific response assertion. The assertion must pass on both same-identity test captures and fail on a meaningful control. An equivalent endpoint is acceptable on the same authorized origin when its purpose is stated. Record inferred impact separately and attach receipt IDs to completed security_coverage checks. If the required proof cannot be captured within policy, keep the claim as a candidate while continuing other permitted tests.
