"""Cache-erosion monitor (FRE-406 P2 / ADR-0078 D6).

Detects prefix-hash instability for registered callsites by comparing
the Jaccard similarity of ``prompt_static_prefix_hash`` values between
consecutive calendar days. Similarity < 0.9 signals erosion.
"""
