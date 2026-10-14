---
credential: ntfy user password
shape:
  words: is 24 URL-safe characters (letters, digits, - and _), as step 1 generates it
  pattern: '[-_A-Za-z0-9]{24}'
expires: false
---
ntfy's auth config takes the user's bcrypt hash, never the password: the leaf keeps the hash
beside the password (operator-hash is derived from operator-password with ntfy user hash), so a
rotation of the password rewrites the hash with it.

1. Generate the new password:
   openssl rand -base64 18 | tr '+/' '-_'
2. Hash it with ntfy user hash, from the ntfy CLI the notes name, which reads the password twice
   on stdin, and write the hash to the leaf's hash key before you enter the password here: this
   plan's rollout restarts ntfy, which then takes the new hash.
3. Enter the password here.
4. Once this plan has finished, log in again with it wherever the notes say the user logs in.
