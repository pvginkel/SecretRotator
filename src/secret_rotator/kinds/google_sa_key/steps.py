"""The google-sa-key kind's own steps (design §4.2: custom steps are the plugin's):
google_sa_key.mint, google_sa_key.prove and google_sa_key.delete, over Google's IAM API, each
logged in as the service account with a key file of the leaf's: the one it held before the plan,
or the one the plan created. No detail or error they report carries a key's private half; an id
names a key without being one."""

from secret_rotator.kinds.google_sa_key.google import Account, Google, GoogleError, Key, parse
from secret_rotator.model import Context, Step, StepFailed, not_landed, value_name, wait

# The staging name of the id of the key the leaf held, read from its key file.
OLD = "google-sa-key:old"
# Google may refuse a new key for 60 seconds or more after its create (IAM's documentation of
# keys.create); the proof asks again for this long.
PROVE_BOUND, PROVE_POLL = 300, 10  # seconds


def held_text(ctx: Context, leaf: str, key: str) -> str:
    version = ctx.bao.read(leaf)
    text = None if version is None else version.data.get(key)
    if not text:
        raise StepFailed(f"{leaf}#{key} holds no key file")
    return text


def held(ctx: Context, leaf: str, key: str) -> Key:
    """The key the leaf's key file holds."""
    try:
        return parse(held_text(ctx, leaf, key))
    except ValueError as e:
        raise StepFailed(f"{leaf}#{key} holds no service account key file: {e}") from None


def login(google: Google, key: Key, what: str) -> Account:
    """The key's account, logged in with the key, described as what."""
    try:
        return google.login(key)
    except GoogleError as e:
        if not e.refused:
            raise
        raise StepFailed(f"Google refuses {what}: {e}") from None


def refused(e: Exception) -> bool:
    return isinstance(e, GoogleError) and e.refused


class Mint(Step):
    """Creates a new key of the service account the leaf's key file names, logged in with that
    key, and stages the new key file Google answers. First it stages the id of the key the leaf
    holds, the one the delete ends.

    A re-run with a key file staged creates none. A create whose answer is lost leaves a key no one
    holds, since only that answer carries its private half, and a re-run creates another. Its undo
    deletes the key it created, logged in with the key the leaf holds, which a rollback has put
    back by then. Every failure before the create, and the create refused (a 4xx answer, as to an
    account that may not key itself), report that the step did not land."""

    type = "google_sa_key.mint"
    mutates = True

    def __init__(self, google: Google, leaf: str, key: str):
        super().__init__("google_sa_key.mint", "create a new key of the service account")
        self.google = google
        self.leaf = leaf
        self.key = key

    @property
    def _what(self) -> str:
        return f"the key {self.leaf}#{self.key} holds"

    def run(self, ctx: Context) -> str:
        if ctx.staged(value_name(self.key)) is None:
            sending = False
            try:
                old = held(ctx, self.leaf, self.key)
                if ctx.staged(OLD) is None:
                    ctx.stage(OLD, old.id)
                account = login(self.google, old, self._what)
                sending = True
                text = account.create()
            except Exception as e:
                if not sending or refused(e):
                    raise not_landed(e) from e
                raise
            ctx.stage(value_name(self.key), text)
        new = parse(ctx.staged(value_name(self.key)))
        return f"key {new.id} of {new.email}"

    def undo(self, ctx: Context) -> str:
        text = ctx.staged(value_name(self.key))
        if text is None:
            return "nothing was created"
        new = parse(text).id
        account = login(self.google, held(ctx, self.leaf, self.key), self._what)
        if new not in account.key_ids():
            return f"Google lists no key {new}: nothing to delete"
        try:
            account.delete(new)
        except GoogleError as e:
            if e.status != 404:
                raise
        return f"deleted key {new}"


class Prove(Step):
    """Logs in as the service account with the key file the leaf holds, which must be the one the
    plan created: Google must answer the assertion the new key signs with an access token. While
    Google refuses it, the step asks again, for PROVE_BOUND at most."""

    type = "google_sa_key.prove"
    silent = True

    def __init__(self, google: Google, leaf: str, key: str):
        super().__init__("google_sa_key.prove", "log in with the new key")
        self.google = google
        self.leaf = leaf
        self.key = key

    def run(self, ctx: Context) -> str:
        text = ctx.staged(value_name(self.key))
        if text is None:
            raise StepFailed("no new key is staged")
        if held_text(ctx, self.leaf, self.key) != text:
            raise StepFailed(f"{self.leaf}#{self.key} does not hold the key the plan created")
        new = parse(text)

        def why_not() -> str | None:
            try:
                self.google.login(new)
            except GoogleError as e:
                if not e.refused:
                    raise
                return f"Google refuses it: {e}"
            return None

        wait(
            self.google, ctx, PROVE_BOUND, PROVE_POLL, why_not, f"Google did not take key {new.id}"
        )
        return f"logged in as {new.email} with key {new.id}"


class Delete(Step):
    """Deletes the key the leaf held before the plan, by the id the mint staged from the leaf's
    key file, and no other, logged in with the key the plan created. It verifies by listing the
    account's keys again. The plan puts it after the proof of the new key.

    It has no undo. A failure before its delete, and that delete refused (a 4xx answer but 404,
    which says the key is gone), report that the step did not land."""

    type = "google_sa_key.delete"
    mutates = True

    def __init__(self, google: Google, key: str):
        super().__init__("google_sa_key.delete", "delete the key the leaf held")
        self.google = google
        self.key = key
        self.no_undo = "a deleted Google service account key cannot be restored"

    def run(self, ctx: Context) -> str:
        sending = False
        try:
            old, text = ctx.staged(OLD), ctx.staged(value_name(self.key))
            if old is None or text is None:
                raise StepFailed("no new key is staged")
            account = login(self.google, parse(text), "the new key")
            if old in account.key_ids():
                sending = True
                try:
                    account.delete(old)
                except GoogleError as e:
                    if e.status != 404:
                        raise
            left = account.key_ids()
        except Exception as e:
            if not sending or refused(e):
                raise not_landed(e) from e
            raise
        if old in left:
            raise StepFailed(f"Google still lists key {old} after its delete")
        if not sending:
            return f"key {old} was deleted already"
        return f"deleted key {old}"
