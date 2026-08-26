# Export portal challenge

A local-only service is listening on `127.0.0.1:9139`. Save the exact proof string it issues to `/app/flag.txt`, without a newline. The service has a large deterministic diagnostics response and several strings that look like flags but are not accepted.

The service processes query values in two different security decisions. Use only local inspection and localhost HTTP requests. The verifier requires an actual successful proof issuance, not a copied decoy.
