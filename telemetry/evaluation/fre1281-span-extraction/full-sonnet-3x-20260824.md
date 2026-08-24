# FRE-1281 span extraction — full-sonnet-3x-20260824

- partition: **full corpus**
- documents: 156
- extractor: span_extraction role -> claude_sonnet
- degraded documents (post-pass failed closed): 9

## Bars

| bar | observed | required | met |
| --- | --- | --- | --- |
| `recall.overall` | 0.917 | >= 0.9 | yes |
| `precision.overall` | 0.875 | >= 0.8 | yes |
| `decomposition.boundary_f1` | 0.773 | >= 0.75 | yes |
| `recall.class.checkable_evaluative` | 0.846 | >= 0.85 | NO |
| `recall.class.dependency_declaration` | 1.000 | >= 0.85 | yes |
| `recall.class.factual_bare_predicate` | 0.929 | >= 0.85 | yes |
| `recall.class.factual_entity` | 0.718 | >= 0.85 | NO |
| `recall.class.nl_in_code` | 1.000 | >= 0.85 | yes |
| `recall.class.prose_about_code` | 1.000 | >= 0.85 | yes |
| `recall.class.prose_in_fence` | 1.000 | >= 0.85 | yes |
| `recall.class.unattributed_restatement` | 0.900 | >= 0.85 | yes |
| `fp_rate.class.attributed_restatement` | 0.000 | <= 0.15 | yes |
| `fp_rate.class.code_body` | 0.033 | <= 0.15 | yes |
| `fp_rate.class.connective_evaluative` | 0.233 | <= 0.15 | NO |
| `fp_rate.class.derived_arithmetic` | 0.000 | <= 0.15 | yes |
| `fp_rate.class.system_record` | 0.000 | <= 0.15 | yes |

**Verdict: FAIL**

Unmet bars, each with the failure it was preregistered to prevent:

- `recall.class.checkable_evaluative` — A class-shaped hole is invisible in an overall figure. checkable_evaluative must clear the bar on its own (ADR-0138 AC-7).
- `recall.class.factual_entity` — A class-shaped hole is invisible in an overall figure. factual_entity must clear the bar on its own (ADR-0138 AC-7).
- `fp_rate.class.connective_evaluative` — Sweeping connective_evaluative into the contract manufactures refusals the user did not deserve (ADR-0138 AC-3, D7).

## Per-document

| doc | gold claims | predicted | matched |
| --- | --- | --- | --- |
| d01-tuna-brands | 3 | 3 | 3 |
| d02-mercury-bare | 3 | 4 | 3 |
| d03-storage-bare | 4 | 3 | 3 |
| d04-city-conjunction | 3 | 3 | 3 |
| d05-rainfall | 3 | 3 | 3 |
| d06-bare-predicates-cooking | 3 | 3 | 3 |
| d07-bare-predicate-mixed | 3 | 3 | 3 |
| d08-httpx-client | 4 | 3 | 2 |
| d09-print-paris | 2 | 5 | 2 |
| d10-comment-claims | 3 | 4 | 3 |
| d11-js-comment | 2 | 2 | 1 |
| d12-install-commands | 2 | 2 | 2 |
| d13-manifest | 3 | 5 | 2 |
| d14-imports-many | 4 | 6 | 4 |
| d15-sql-body | 3 | 2 | 1 |
| d16-code-body-only | 4 | 1 | 0 |
| d17-yaml-body | 2 | 1 | 0 |
| d18-text-fence | 3 | 3 | 3 |
| d19-mislabelled-fence | 3 | 3 | 3 |
| d20-unfenced-language | 2 | 2 | 2 |
| d21-fence-mixed | 2 | 2 | 2 |
| d22-api-claims | 3 | 3 | 3 |
| d23-api-claims-two | 3 | 3 | 3 |
| d24-api-claims-three | 2 | 2 | 2 |
| d25-well-regarded | 3 | 3 | 3 |
| d26-recommended | 3 | 2 | 1 |
| d27-standard-approach | 3 | 2 | 2 |
| d28-connective-over-cited | 4 | 4 | 3 |
| d29-connective-more | 3 | 3 | 2 |
| d30-connective-ordering | 3 | 2 | 1 |
| d31-restatement-overlap | 2 | 3 | 2 |
| d32-restatement-plain | 2 | 2 | 2 |
| d33-restatement-more | 3 | 2 | 2 |
| d34-unattributed | 3 | 3 | 3 |
| d35-unattributed-more | 2 | 2 | 2 |
| d36-restatement-mixed | 4 | 4 | 4 |
| d37-unattributed-three | 3 | 2 | 2 |
| d38-arithmetic | 4 | 4 | 3 |
| d39-arithmetic-more | 3 | 3 | 1 |
| d40-arithmetic-three | 3 | 3 | 2 |
| d41-no-source | 4 | 2 | 2 |
| d42-system-record-more | 4 | 3 | 3 |
| d43-system-record-vs-world | 2 | 2 | 2 |
| d44-mixed-close | 0 | 0 | 0 |
| d45-mixed-heavy | 5 | 5 | 5 |
| d46-docstring-claims | 4 | 7 | 2 |
| d47-string-literal-claims | 4 | 10 | 3 |
| d48-shell-comment | 2 | 2 | 2 |
| d49-yaml-comment | 2 | 2 | 1 |
| d50-restatement-and-record | 4 | 3 | 3 |
| d51-arithmetic-and-ordering | 3 | 3 | 2 |
| d52-unattributed-and-dep | 2 | 2 | 2 |
| d01-tuna-brands | 3 | 4 | 3 |
| d02-mercury-bare | 3 | 4 | 3 |
| d03-storage-bare | 4 | 3 | 3 |
| d04-city-conjunction | 3 | 3 | 3 |
| d05-rainfall | 3 | 3 | 3 |
| d06-bare-predicates-cooking | 3 | 3 | 3 |
| d07-bare-predicate-mixed | 3 | 3 | 3 |
| d08-httpx-client | 4 | 3 | 2 |
| d09-print-paris | 2 | 5 | 2 |
| d10-comment-claims | 3 | 4 | 3 |
| d11-js-comment | 2 | 2 | 1 |
| d12-install-commands | 2 | 2 | 2 |
| d13-manifest | 3 | 5 | 2 |
| d14-imports-many | 4 | 6 | 4 |
| d15-sql-body | 3 | 2 | 1 |
| d16-code-body-only | 4 | 1 | 0 |
| d17-yaml-body | 2 | 1 | 0 |
| d18-text-fence | 3 | 3 | 3 |
| d19-mislabelled-fence | 3 | 3 | 3 |
| d20-unfenced-language | 2 | 2 | 2 |
| d21-fence-mixed | 2 | 2 | 2 |
| d22-api-claims | 3 | 3 | 3 |
| d23-api-claims-two | 3 | 3 | 3 |
| d24-api-claims-three | 2 | 2 | 2 |
| d25-well-regarded | 3 | 3 | 3 |
| d26-recommended | 3 | 2 | 1 |
| d27-standard-approach | 3 | 3 | 3 |
| d28-connective-over-cited | 4 | 4 | 3 |
| d29-connective-more | 3 | 3 | 2 |
| d30-connective-ordering | 3 | 2 | 1 |
| d31-restatement-overlap | 2 | 3 | 2 |
| d32-restatement-plain | 2 | 2 | 2 |
| d33-restatement-more | 3 | 3 | 3 |
| d34-unattributed | 3 | 3 | 3 |
| d35-unattributed-more | 2 | 2 | 2 |
| d36-restatement-mixed | 4 | 4 | 4 |
| d37-unattributed-three | 3 | 2 | 2 |
| d38-arithmetic | 4 | 4 | 3 |
| d39-arithmetic-more | 3 | 3 | 1 |
| d40-arithmetic-three | 3 | 3 | 2 |
| d41-no-source | 4 | 2 | 2 |
| d42-system-record-more | 4 | 3 | 3 |
| d43-system-record-vs-world | 2 | 2 | 2 |
| d44-mixed-close | 0 | 0 | 0 |
| d45-mixed-heavy | 5 | 5 | 5 |
| d46-docstring-claims | 4 | 7 | 2 |
| d47-string-literal-claims | 4 | 10 | 3 |
| d48-shell-comment | 2 | 2 | 2 |
| d49-yaml-comment | 2 | 2 | 1 |
| d50-restatement-and-record | 4 | 3 | 3 |
| d51-arithmetic-and-ordering | 3 | 3 | 2 |
| d52-unattributed-and-dep | 2 | 2 | 2 |
| d01-tuna-brands | 3 | 3 | 3 |
| d02-mercury-bare | 3 | 4 | 3 |
| d03-storage-bare | 4 | 3 | 3 |
| d04-city-conjunction | 3 | 3 | 3 |
| d05-rainfall | 3 | 3 | 3 |
| d06-bare-predicates-cooking | 3 | 3 | 3 |
| d07-bare-predicate-mixed | 3 | 3 | 3 |
| d08-httpx-client | 4 | 3 | 2 |
| d09-print-paris | 2 | 5 | 2 |
| d10-comment-claims | 3 | 4 | 3 |
| d11-js-comment | 2 | 2 | 1 |
| d12-install-commands | 2 | 2 | 2 |
| d13-manifest | 3 | 5 | 2 |
| d14-imports-many | 4 | 6 | 4 |
| d15-sql-body | 3 | 2 | 1 |
| d16-code-body-only | 4 | 1 | 0 |
| d17-yaml-body | 2 | 1 | 0 |
| d18-text-fence | 3 | 3 | 3 |
| d19-mislabelled-fence | 3 | 3 | 3 |
| d20-unfenced-language | 2 | 2 | 2 |
| d21-fence-mixed | 2 | 2 | 2 |
| d22-api-claims | 3 | 3 | 3 |
| d23-api-claims-two | 3 | 3 | 3 |
| d24-api-claims-three | 2 | 2 | 2 |
| d25-well-regarded | 3 | 2 | 2 |
| d26-recommended | 3 | 3 | 3 |
| d27-standard-approach | 3 | 3 | 3 |
| d28-connective-over-cited | 4 | 4 | 3 |
| d29-connective-more | 3 | 3 | 2 |
| d30-connective-ordering | 3 | 2 | 1 |
| d31-restatement-overlap | 2 | 3 | 2 |
| d32-restatement-plain | 2 | 2 | 2 |
| d33-restatement-more | 3 | 3 | 3 |
| d34-unattributed | 3 | 3 | 3 |
| d35-unattributed-more | 2 | 2 | 2 |
| d36-restatement-mixed | 4 | 4 | 4 |
| d37-unattributed-three | 3 | 2 | 2 |
| d38-arithmetic | 4 | 4 | 3 |
| d39-arithmetic-more | 3 | 3 | 1 |
| d40-arithmetic-three | 3 | 3 | 2 |
| d41-no-source | 4 | 3 | 3 |
| d42-system-record-more | 4 | 3 | 3 |
| d43-system-record-vs-world | 2 | 2 | 2 |
| d44-mixed-close | 0 | 0 | 0 |
| d45-mixed-heavy | 5 | 5 | 5 |
| d46-docstring-claims | 4 | 6 | 2 |
| d47-string-literal-claims | 4 | 10 | 3 |
| d48-shell-comment | 2 | 2 | 2 |
| d49-yaml-comment | 2 | 2 | 1 |
| d50-restatement-and-record | 4 | 3 | 3 |
| d51-arithmetic-and-ordering | 3 | 3 | 2 |
| d52-unattributed-and-dep | 2 | 2 | 2 |

> 3 samples per document. Overall recall per sample: 0.902, 0.924, 0.924 (min 0.902, max 0.924). The bars above are scored over all samples pooled.
