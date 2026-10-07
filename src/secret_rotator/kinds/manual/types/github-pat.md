---
credential: GitHub personal access token
shape:
  words: starts with ghp_ or github_pat_
  pattern: (ghp|github_pat)_[A-Za-z0-9_]+
expires: true
---
1. Sign in to GitHub as the account the notes name and open Settings → Developer settings →
   Personal access tokens (https://github.com/settings/tokens).
2. Generate a new token of the sort the notes name, classic or fine-grained, with exactly the
   scopes, resource owner and repositories they give, and an expiration.
3. Copy the token (GitHub shows it once) and enter it here.
4. Delete the old token on the same page once this plan has finished: its consumers use it until
   then.
