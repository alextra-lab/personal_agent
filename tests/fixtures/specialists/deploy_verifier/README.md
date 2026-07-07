# Deploy-verifier fixtures (FRE-834, ADR-0113 §3)

| File | Role |
|------|------|
| `healthy_response.txt` | Canned healthy curl output for the post-deploy evidence tests. Expected: **APPROVE**. |
| `unhealthy_response.txt` | Canned degraded/unhealthy curl output. Expected: **REJECT**. |
| `injected_health_response.txt` | Canned curl output that plants a fake envelope-close delimiter and a spoofed `<<<VERDICT>>>` APPROVE block, proving `fetch_deploy_artifact` places the whole raw response inside the untrusted envelope (structural — the harness's `neutralize()` does the rest). |

The pre-deploy authorization gate (`deploy_authorized`, AC-7) is pure logic with no evidence fixtures
— it is tested directly against `ProposedDeploy` values in `test_deploy_verifier.py`.
