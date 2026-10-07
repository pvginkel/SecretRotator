---
credential: Grafana service account token
shape:
  words: starts with glsa_
  pattern: glsa_[A-Za-z0-9_]+
expires: true
---
1. Sign in to Grafana as an administrator and open Administration → Users and access → Service
   accounts.
2. Open the service account the notes name, Add service account token, with an expiration or none,
   Generate, and copy it (Grafana shows it once).
3. Enter the token here.
4. Delete the old token from the service account once this plan has finished: its consumers use it
   until then.
