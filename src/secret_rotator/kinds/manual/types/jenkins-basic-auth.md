---
credential: Jenkins API token header
shape:
  words: is Basic, a space and base64
  pattern: Basic [A-Za-z0-9+/]+={0,2}
expires: false
---
The value is a whole HTTP Authorization header: Basic, a space, then the base64 of
<user>:<API token>.

1. Sign in to Jenkins as the user the notes name and open the user's Security page (the user's name
   → Security → API Token).
2. Add new Token, named after the consumer, Generate, and copy it (Jenkins shows it once).
3. Build the header: Basic, a space, and what printf '%s' '<user>:<API token>' | base64 -w0
   prints.
4. Enter the whole header here.
5. Revoke the old token on the same page once this plan has finished: its consumer uses it until
   then.
