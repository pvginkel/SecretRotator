---
credential: SSH private key
shape:
  words: is a private key, from -----BEGIN … PRIVATE KEY----- to -----END … PRIVATE KEY-----
  pattern: '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----.+-----END ([A-Z]+ )?PRIVATE KEY-----\s*'
  multiline: true
expires: false
---
1. Generate a new key pair, with the comment the notes name, into a temporary file:
   ssh-keygen -t ed25519 -N '' -C <comment> -f <file>
2. Install the public half (<file>.pub) everywhere the notes say the key logs in, beside the old
   one, and where the notes say it is kept.
3. Enter the private half here, the whole file from -----BEGIN to -----END.
4. Once this plan has finished, remove the old public half where it was installed and delete the
   temporary files.
