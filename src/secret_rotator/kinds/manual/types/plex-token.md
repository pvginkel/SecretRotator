---
credential: Plex authentication token
shape:
  words: is 20 letters, digits, dashes or underscores (an X-Plex-Token)
  pattern: '[A-Za-z0-9_-]{20}'
expires: false
---
1. Sign in to Plex Web (https://app.plex.tv) as the account the consumer uses, open any item's
   menu, pick Get Info, then View XML.
2. Copy the X-Plex-Token value from the address of the XML page that opens.
3. Enter the token here.
4. Plex hands out one token per signed-in device: once this plan has finished, remove the device
   that held the old token under Account settings, Authorized Devices, which ends the old token.
