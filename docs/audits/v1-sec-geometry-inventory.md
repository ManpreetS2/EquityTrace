# Official SEC geometry inventory

Measured at source SHA `e1e88c706e5020e43d424b0440eddbb0e645a3b0`. Acquisition completed 2026-10-27 UTC.

97 official responses: 1 ticker mapping, 8 primary submissions, 80 referenced archives and 8 Company Facts. All 97 stored wire hashes verified. Wire bytes: 6,346,557; decoded bytes: 65,072,833. Raw responses remain external and are not committed.

| Security | Official CIK | Facts | Filings | Concepts | Unit groups | Expanded typed string bytes |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| AAPL | 0000320193 | 25135 | 2270 | 505 | 508 | 11383159 |
| MSFT | 0000789019 | 32711 | 4531 | 572 | 577 | 14351361 |
| NVDA | 0001045810 | 27281 | 2489 | 644 | 647 | 12369351 |
| JPM | 0000019617 | 54172 | 169637 | 936 | 938 | 23768929 |
| XOM | 0002115436 | 280 | 36 | 101 | 101 | 133122 |
| JNJ | 0000200406 | 27849 | 3374 | 612 | 617 | 12918755 |
| F | 0000037996 | 24139 | 4447 | 592 | 589 | 10662175 |
| BRK.B | 0001067983 | 18233 | 2405 | 447 | 449 | 8413681 |

All raw observation and normalized fact counts agree; all normalized facts matched retrieved filing accessions and had acceptance timestamps/available_at. This is coverage, not an independent timestamp-correctness proof. No missing/null raw accession, end, filed or value fields were observed. Raw/decoded source hashes, URLs and retrieval times are in [source metadata](v1-sec-geometry-sources.json).

Official mapping resolves XOM to CIK 0002115436. Its 280 observations do not establish exhaustive historical XOM coverage. No alternate issuer was substituted. BRK.B maps to official BRK-B; security/share-class identity remains distinct from issuer identity.

Five issuers exceed the 25,000-fact synthetic qualification. JPM exceeds the existing 50,000-filing admission limit. Every issuer except XOM exceeds the diverse profile target of 500 filings. Real geometry does not establish process RSS, constrained-host support or production limits. Complete retrieved API responses do not mean exhaustive historical SEC evidence or historical provider vintages.

The initial external diagnostic AttributeError was corrected and fetched responses reused after hash verification. A repeated JPM accession-set construction made the diagnostic slow; its runtime is not an ingestion benchmark.

Next: review historical identity coverage and approve one finite joint-dimension capacity qualification including retained database history. Crash qualification remains separately unauthorized. See the development handoff for the private evidence transfer inventory.
