---
credential: TorGuard WireGuard config
shape:
  words: is a WireGuard config, an [Interface] section with its PrivateKey and a [Peer] section
  pattern: .*\[Interface\].*PrivateKey.*\[Peer\].*
  multiline: true
expires: false
---
The value is the whole config file, from [Interface] to the end of [Peer].

1. Sign in to the TorGuard control panel and open its WireGuard config generator.
2. Generate a new WireGuard config, for the server and settings the notes name.
3. Enter the whole file here.
