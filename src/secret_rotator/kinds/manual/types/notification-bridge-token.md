---
credential: notification bridge registration token
shape:
  words: is 43 URL-safe characters (letters, digits, - and _), as step 1 generates it
  pattern: '[-_A-Za-z0-9]{43}'
expires: false
---
The bridge checks a sender's bearer token against its registration's key in this leaf. This type
is for a sender that holds its token outside OpenBao, where no copy can carry a rotation to it.

1. Generate the new token:
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'
2. Enter the token here. This plan's rollout restarts the bridge, which then refuses the old token.
3. Once this plan has finished, put the new token where the notes say the sender holds it: until
   then the sender's notifications are refused.
