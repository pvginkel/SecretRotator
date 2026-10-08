---
credential: Trello API key or token
shape:
  words: is 32 hexadecimal digits (a key), or 64 of them or ATTA and hexadecimal digits (a token)
  pattern: '[0-9A-Fa-f]{32}|[0-9A-Fa-f]{64}|ATTA[0-9A-Fa-f]+'
expires: false
---
1. Sign in to Trello, open https://trello.com/power-ups/admin and pick the Power-Up whose API key
   the consumer uses.
2. For the API key: create a new one on the Power-Up's API key page. For the token: authorize the
   API key again from that page's Token link, with no expiry, and allow it.
3. Copy the new key or token and enter it here.
4. Revoke the old one once this plan has finished: its consumer uses it until then.
