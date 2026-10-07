---
credential: Argo CD API token
shape:
  words: is a JWT, three base64url parts joined by dots, starting eyJ
  pattern: eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+
expires: true
---
1. Log in to Argo CD as admin with the argocd CLI.
2. Mint a token for the account the notes name:
   argocd account generate-token --account <account>
   adding --expires-in <duration> for a token that expires.
3. Enter the token here.
4. Delete the old token once this plan has finished, its consumers use it until then:
   argocd account get --account <account> lists the tokens with their ids, and
   argocd account delete-token --account <account> <id> deletes one.
