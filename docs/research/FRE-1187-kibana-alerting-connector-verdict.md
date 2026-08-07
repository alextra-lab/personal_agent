# FRE-1187 — Kibana alerting connector + rule-expressiveness verdict

**Ticket:** FRE-1187 (ADR-0134 T1). **Backing ADR:** ADR-0134 D2a — this is the ADR's first
implementation step, gating the whole Kibana stage. **Date:** 2026-08-07.

## What was done

`xpack.encryptedSavedObjects.encryptionKey` was set for the live `cloud-sim-kibana` container
(8.19.0) and the container was recreated. Confirmed live, immediately after recreate:

- **AC-1** — `docker logs cloud-sim-kibana` since the recreate carries none of the three
  `plugins.encryptedSavedObjects` / `plugins.actions` / `plugins.alerting` disabled-key warnings
  that were present in the pre-change baseline (`docker logs` before recreate showed them
  recurring roughly weekly since deployment; see ADR-0134 Context for the original transcript).
- **AC-2** — `GET /api/actions/connector_types` → HTTP 200, body is a 29-element JSON array (was
  HTTP 500 before the key was set).
- Elasticsearch licence, confirmed live via `GET /_license`: `"type": "basic"`, `"status": "active"`.

## AC-3 — Connector inventory and licence flags

All 29 connector types returned by `/api/actions/connector_types`, with the licence-relevant
fields the endpoint actually returns (`enabled`, `enabled_in_license`, `minimum_license_required`):

| id | name | enabled | enabled_in_license | minimum_license_required |
|---|---|---|---|---|
| `.index` | Index | **true** | **true** | basic |
| `.server-log` | Server log | **true** | **true** | basic |
| `.email` | Email | false | false | gold |
| `.pagerduty` | PagerDuty | false | false | gold |
| `.swimlane` | Swimlane | false | false | gold |
| `.slack` | Slack | false | false | gold |
| `.slack_api` | Slack API | false | false | gold |
| `.webhook` | Webhook | false | false | gold |
| `.cases-webhook` | Webhook - Case Management | false | false | gold |
| `.xmatters` | xMatters | false | false | gold |
| `.jira` | Jira | false | false | gold |
| `.teams` | Microsoft Teams | false | false | gold |
| `.torq` | Torq | false | false | gold |
| `.tines` | Tines | false | false | gold |
| `.d3security` | D3 Security | false | false | gold |
| `.servicenow` | ServiceNow ITSM | false | false | platinum |
| `.servicenow-sir` | ServiceNow SecOps | false | false | platinum |
| `.servicenow-itom` | ServiceNow ITOM | false | false | platinum |
| `.opsgenie` | Opsgenie | false | false | platinum |
| `.resilient` | IBM Resilient | false | false | platinum |
| `.thehive` | TheHive | false | false | platinum |
| `.xsoar` | XSOAR | false | false | platinum |
| `.gen-ai` | OpenAI | false | false | enterprise |
| `.bedrock` | Amazon Bedrock | false | false | enterprise |
| `.gemini` | Google Gemini | false | false | enterprise |
| `.sentinelone` | Sentinel One | false | false | enterprise |
| `.crowdstrike` | CrowdStrike | false | false | enterprise |
| `.inference` | AI Connector | false | false | enterprise |
| `.microsoft_defender_endpoint` | Microsoft Defender for Endpoint | false | false | enterprise |

**Out-of-box conclusion: NO.** Exactly two connector types are enabled under this `basic` licence
— `.index` and `.server-log` — and both were already named in ADR-0134's Context as the ones that
"do not [leave the box] — an alert that writes an index document notifies nobody." Every connector
that reaches outside the stack (email, Slack, PagerDuty, webhook, Jira, Teams, …) requires at
minimum a `gold` licence; none is enabled here. This is not a partial or ambiguous result — the
enabled set is precisely the two the ADR flagged as insufficient, with zero exceptions.

## AC-4 — Rule-expressiveness verdict

`/api/alerting/rule_types` returns 47 rule types. The one capable of an arbitrary query against
Elasticsearch — the only kind of rule that could compute a trailing baseline against a separate
denominator — is **`.es-query`** ("Elasticsearch query"), confirmed available under this licence:
`"minimum_license_required": "basic"`, `"enabled_in_license": true`.

Its params (from the same API response) include `esQuery` ("The string representation of the
Elasticsearch query") and `esqlQuery`, i.e. it accepts an arbitrary Elasticsearch Query DSL or
ES|QL body, not just a fixed metric-vs-static-threshold form. Elasticsearch's own aggregation
framework supports pipeline aggregations (`moving_fn`, `serial_diff`, `bucket_script`) that can
compute a value like "current bucket vs. a trailing average of N prior buckets" and combine it
with a second sibling aggregation's value within **one** query — which is the shape rule 1's
shortfall branch needs (dynamic trailing baseline ÷ separately sourced denominator).

**Verdict: expressible in principle, with a precondition ADR-0134 D4 already names and this
investigation confirms is currently unmet.** The computation has to live inside a single
Elasticsearch query, because Kibana rules query only Elasticsearch (D4: "Kibana queries only
Elasticsearch, so using [the `api_costs` rate] from the Kibana stage requires that rate to be
present in Elasticsearch by an independent path"). Checked live: `GET /_cat/indices/*cost*` on
this cluster returns **no matching index** — the `api_costs` denominator is not currently indexed
into Elasticsearch at all (it lives in Postgres only). So while `.es-query`'s query surface is
technically capable of expressing the shortfall branch's math, nothing in Elasticsearch today
supplies the second series the math needs.

## AC-6 — Disposition

**Abandon the Kibana stage.** AC-3 alone is decisive: D2a's stated contingency is "if the `basic`
connector set proves to contain nothing that leaves the box — index and server-log connectors
only — then a Kibana alert is a log line, which is the failure this ADR exists to end, and the
Kibana stage is abandoned outright: rules 1 and 2 wait for FRE-1072 with the rest." That is exactly
what was measured. AC-4's conditional-yes on rule expressiveness does not change the outcome —
notification capability gates everything before expressiveness is relevant, and it gates closed.

Per D2a: rules 1 and 2 wait for FRE-1072 (ADR-0129 B7, Tempo + Grafana) with the rest of the alert
set. No half-measure is authorized — no rule is authored against `.index`/`.server-log` as an
interim step.

## What this leaves unresolved for FRE-1072

Not this ticket's job to close, recorded so the next ticket doesn't have to re-derive it: the
`api_costs` denominator has no path into Elasticsearch today. Grafana can query Postgres directly
(ADR-0134 References cite FRE-1039, Grafana over Postgres for cost/ledger truth), so this may be a
non-issue once the destination changes — but it means rule 1's shortfall branch was never actually
blocked by Kibana's query engine, only by Kibana's connector licence and (secondarily) by data not
yet reaching Elasticsearch.
